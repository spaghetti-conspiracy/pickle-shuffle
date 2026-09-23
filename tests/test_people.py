"""メンバー台帳。練習会から切り離したことで守りたい性質を見る。"""

from __future__ import annotations

import pytest

from app.errors import NotFoundError
from app.models import Member, Owner, Person
from app.scheduler.domain import Gender, Level, MemberStatus, RoundStatus
from app.services import people as people_service
from app.services import rounds as rounds_service
from app.services import sessions as sessions_service
from app.services import stats as stats_service
from app.services.owners import current_owner


def _person(db, owner, nickname, level=Level.PICKLEBALL, gender=Gender.MALE):
    return people_service.add_person(
        db, owner, nickname=nickname, gender=gender, level=level
    )


def _session_with(db, owner, count=8, name="練習会"):
    session = sessions_service.create_session(db, owner, name, 2)
    members = [
        sessions_service.add_member(db, session, _person(db, owner, f"m{i + 1}"))
        for i in range(count)
    ]
    return session, members


# ---------------------------------------------------------------------------
# 練習会から外すことと、台帳から消すことは別
# ---------------------------------------------------------------------------


def test_removing_from_a_session_keeps_the_person(db, owner):
    """練習会から外しても、台帳には残る。次の週にまた呼べる。"""
    session, members = _session_with(db, owner, count=4)
    sessions_service.remove_member(db, members[0])

    assert [m.nickname for m in sessions_service.list_members(db, session.id)] == [
        "m2",
        "m3",
        "m4",
    ]
    assert "m1" in {person.nickname for person in people_service.list_people(db, owner)}


def test_removing_someone_who_played_keeps_the_record(db, owner):
    """出場済みの人を外したら「離脱」。記録は残す（統計が壊れるため）。"""
    session, members = _session_with(db, owner)
    rounds_service.adopt(db, rounds_service.generate(db, session))
    played = sum(stats_service.play_counts(db, session.id).values())

    sessions_service.remove_member(db, members[0])
    db.refresh(members[0])

    assert members[0].status is MemberStatus.LEFT
    assert sum(stats_service.play_counts(db, session.id).values()) == played
    assert "m1" in {person.nickname for person in people_service.list_people(db, owner)}


def test_deleting_a_person_does_not_touch_the_sessions(db, owner):
    """台帳から消しても、参加者・記録・進行中のマッチは動かない。

    進行中の練習会からいきなり人が抜けると事故になる。切れるのは台帳への
    紐づけだけ。
    """
    session, members = _session_with(db, owner)
    pending = rounds_service.generate(db, session)
    rounds_service.adopt(db, pending)
    before = [(slot.member_id, slot.team_index) for m in pending.matches for slot in m.slots]

    person = db.get(Person, members[0].person_id)
    people_service.delete_person(db, owner, person)

    db.refresh(members[0])
    assert members[0].person_id is None, "紐づけだけが切れる"
    assert members[0].nickname == "m1", "名前は写しなので残る"
    assert len(sessions_service.list_members(db, session.id)) == 8
    after = [(slot.member_id, slot.team_index) for m in pending.matches for slot in m.slots]
    assert after == before, "表示中のマッチが動いている"
    assert pending.status is RoundStatus.ADOPTED


def test_a_deleted_person_is_gone_for_good(db, owner):
    """台帳の削除は本当に消す。ここは「外す」ではない。"""
    person = _person(db, owner, "きえる")
    people_service.delete_person(db, owner, person)
    assert people_service.list_people(db, owner) == []


# ---------------------------------------------------------------------------
# 台帳を直したときの反映
# ---------------------------------------------------------------------------


def test_fixing_the_register_reaches_the_session(db, owner):
    """台帳でレベルを直すと、参加者にも反映される。直す場所は1つでよい。"""
    session, members = _session_with(db, owner, count=4)
    person = db.get(Person, members[0].person_id)

    people_service.update_person(db, owner, person, level=Level.BEGINNER)

    db.refresh(members[0])
    assert members[0].level is Level.BEGINNER


def test_fixing_the_register_does_not_move_the_displayed_match(db, owner):
    """生成済みのマッチは動かさない。反映は次の生成から（不変則12）。"""
    session, members = _session_with(db, owner)
    pending = rounds_service.generate(db, session)
    before = [(slot.member_id, slot.team_index) for m in pending.matches for slot in m.slots]

    person = db.get(Person, members[0].person_id)
    people_service.update_person(db, owner, person, level=Level.BEGINNER)

    after = [(slot.member_id, slot.team_index) for m in pending.matches for slot in m.slots]
    assert after == before


def test_renaming_in_the_register_reaches_the_session(db, owner):
    """台帳で改名したら、参加者の呼び名も変わる。"""
    session, members = _session_with(db, owner, count=4)
    person = db.get(Person, members[0].person_id)

    people_service.update_person(db, owner, person, nickname="あたらしい名前")

    db.refresh(members[0])
    assert members[0].nickname == "あたらしい名前"


def test_a_rename_that_collides_gets_a_number(db, owner):
    """改名先が練習会の中で埋まっていたら、番号を振る。"""
    session, members = _session_with(db, owner, count=4)
    person = db.get(Person, members[0].person_id)

    people_service.update_person(db, owner, person, nickname="m2")

    db.refresh(members[0])
    assert members[0].nickname == "m22", "同名のまま2人並んでいる"


def test_a_left_member_is_not_rewritten(db, owner):
    """離脱した行は記録のためにあるので、台帳を直しても書き換えない。"""
    session, members = _session_with(db, owner)
    rounds_service.adopt(db, rounds_service.generate(db, session))
    sessions_service.remove_member(db, members[0])

    person = db.get(Person, members[0].person_id)
    people_service.update_person(db, owner, person, nickname="別名", level=Level.BEGINNER)

    db.refresh(members[0])
    assert (members[0].nickname, members[0].level) == ("m1", Level.PICKLEBALL)


def test_fixing_in_the_session_goes_up_to_the_register(db, owner):
    """練習会側で直した属性は台帳にも上がる。次の練習会でも使える。"""
    session, members = _session_with(db, owner, count=4)
    sessions_service.update_member(db, members[0], level=Level.BEGINNER)

    person = db.get(Person, members[0].person_id)
    assert person.level is Level.BEGINNER


def test_a_session_rename_does_not_go_up(db, owner):
    """練習会の中で付け直した呼び名は、台帳には上げない。

    番号付けや読み上げ用の言い換えで、台帳の名前を書き換えないため。
    """
    session, members = _session_with(db, owner, count=4)
    sessions_service.update_member(db, members[0], nickname="よびかた")

    person = db.get(Person, members[0].person_id)
    assert person.nickname == "m1"


# ---------------------------------------------------------------------------
# 同名と、二重登録の見分け
# ---------------------------------------------------------------------------


def test_the_same_name_gets_a_number_in_the_register(db, owner):
    """同名は禁止しない。選ぶときに迷わないよう番号を振る。"""
    names = [_person(db, owner, "マッツ").nickname for _ in range(3)]
    assert names == ["マッツ", "マッツ2", "マッツ3"]


def test_the_number_follows_the_person_across_sessions(db, owner):
    """番号はその人のものなので、練習会をまたいでも変わらない。"""
    first = _person(db, owner, "マッツ")
    second = _person(db, owner, "マッツ")

    session = sessions_service.create_session(db, owner, "今週", 2)
    member = sessions_service.add_member(db, session, second)

    assert (first.nickname, member.nickname) == ("マッツ", "マッツ2")


def test_hand_registered_people_are_told_apart_from_imported_ones(db, owner):
    """手で登録した人と取り込んだ人を見分けられる。二重登録を消すために要る。"""
    by_hand = _person(db, owner, "だれか")
    imported = people_service.add_person(
        db,
        owner,
        nickname="だれか",
        gender=Gender.MALE,
        level=Level.PICKLEBALL,
        external_id="bear:9001",
    )

    assert people_service.source_label(by_hand) is None
    assert people_service.source_label(imported) == "bear"


# ---------------------------------------------------------------------------
# 団体（マルチテナントの土台）
# ---------------------------------------------------------------------------


def test_another_owner_keeps_its_own_register(db, owner):
    """団体が違えば、同じ名前も同じ取り込み元の人も持てる。見えもしない。"""
    other = Owner(name="よその団体")
    db.add(other)
    db.commit()

    people_service.add_person(
        db, owner, nickname="同じ人", gender=Gender.MALE, level=Level.PICKLEBALL,
        external_id="bear:9001",
    )
    people_service.add_person(
        db, other, nickname="同じ人", gender=Gender.MALE, level=Level.PICKLEBALL,
        external_id="bear:9001",
    )

    assert [p.nickname for p in people_service.list_people(db, owner)] == ["同じ人"]
    assert [p.nickname for p in people_service.list_people(db, other)] == ["同じ人"]


def test_sessions_of_another_owner_are_not_listed(db, owner):
    """練習会の一覧も団体で閉じる。名前の重複も団体の中だけで禁じる。"""
    other = Owner(name="よその団体")
    db.add(other)
    db.commit()

    sessions_service.create_session(db, owner, "金曜練習会", 2)
    sessions_service.create_session(db, other, "金曜練習会", 2)

    assert [s.name for s in sessions_service.list_sessions(db, owner)] == ["金曜練習会"]
    assert [s.name for s in sessions_service.list_sessions(db, other)] == ["金曜練習会"]


def test_a_person_of_another_owner_is_not_reachable(db, owner):
    """よその団体の人は id を知っていても引けない。"""
    other = Owner(name="よその団体")
    db.add(other)
    db.commit()
    person = people_service.add_person(
        db, other, nickname="よその人", gender=Gender.MALE, level=Level.PICKLEBALL
    )

    with pytest.raises(NotFoundError):
        people_service.get_person(db, owner, person.id)


def test_two_admins_share_one_owner(db, owner):
    """同じ団体の管理者が2人いれば、同じ台帳が見える。"""
    from app.models import Admin, OwnerAdmin

    second = Admin(login="another", password_hash="x")
    db.add(second)
    db.flush()
    db.add(OwnerAdmin(owner_id=owner.id, admin_id=second.id))
    db.commit()

    _person(db, owner, "共有の人")
    assert current_owner(db, second).id == owner.id
    assert [p.nickname for p in people_service.list_people(db, current_owner(db, second))] == [
        "共有の人"
    ]


def test_the_session_count_helps_before_deleting(db, owner):
    """いくつの練習会に入っているかを出す。消す前の目安。"""
    session, members = _session_with(db, owner, count=4)
    counts = people_service.session_counts(db, owner)
    assert counts[members[0].person_id] == 1

    sessions_service.remove_member(db, members[0])
    assert people_service.session_counts(db, owner).get(members[0].person_id, 0) == 0


def test_statistics_are_not_shared_through_the_register(db, owner):
    """台帳を共有しても統計は共有しない（不変則13）。"""
    first, members = _session_with(db, owner, name="先週")
    rounds_service.adopt(db, rounds_service.generate(db, first))
    assert sum(stats_service.play_counts(db, first.id).values()) == 8

    second = sessions_service.create_session(db, owner, "今週", 2)
    for member in members:
        sessions_service.add_member(db, second, db.get(Person, member.person_id))

    assert stats_service.play_counts(db, second.id) == {}
    assert all(
        m.baseline == 0 for m in sessions_service.list_members(db, second.id)
    ), "前の練習会の負担を持ち込んでいる"


def test_a_member_row_survives_its_person(db, owner):
    """台帳を消しても、統計の導出は壊れない。"""
    session, members = _session_with(db, owner)
    rounds_service.adopt(db, rounds_service.generate(db, session))
    counts = stats_service.play_counts(db, session.id)

    people_service.delete_person(db, owner, db.get(Person, members[0].person_id))

    assert stats_service.play_counts(db, session.id) == counts
    assert db.get(Member, members[0].id) is not None
