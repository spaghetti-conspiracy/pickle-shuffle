"""ORM モデルの基本的な振る舞い。"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    Court,
    Match,
    MatchSlot,
    Member,
    Owner,
    Person,
    PracticeSession,
    Round,
    RoundParticipation,
)
from app.scheduler.domain import (
    Gender,
    Level,
    MemberStatus,
    ParticipationState,
    RoundStatus,
)


def _owner(db) -> Owner:
    """既定の団体。すべてのデータはここに紐づく。"""
    return db.scalars(select(Owner).order_by(Owner.id)).first()


def _make_session(db, name: str = "練習会", court_count: int = 2) -> PracticeSession:
    s = PracticeSession(owner_id=_owner(db).id, name=name)
    db.add(s)
    db.flush()
    db.add_all([Court(session_id=s.id, court_index=i, name=f"コート{i + 1}") for i in range(court_count)])
    db.commit()
    return s


def test_practice_session_defaults(db):
    s = _make_session(db)
    assert s.random_seed > 0
    assert len(s.token) == 10
    assert [c.name for c in s.courts] == ["コート1", "コート2"]
    assert all(c.in_use for c in s.courts), "作成直後はすべて試合に使う"


def test_courts_can_be_taken_out_of_play_and_brought_back(db):
    """初心者の育成用に、コートを試合から外したり戻したりできる。"""
    s = _make_session(db, court_count=3)
    practice_court = s.courts[2]
    practice_court.in_use = False
    db.commit()
    db.expire_all()

    reloaded = db.scalars(select(PracticeSession)).one()
    assert [c.in_use for c in reloaded.courts] == [True, True, False]

    reloaded.courts[2].in_use = True
    db.commit()
    assert all(c.in_use for c in reloaded.courts)


def test_court_index_is_unique_within_a_session(db):
    s = _make_session(db)
    db.add(Court(session_id=s.id, court_index=0, name="重複"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_courts_are_deleted_with_the_session(db):
    s = _make_session(db, court_count=3)
    db.delete(s)
    db.commit()
    assert db.scalars(select(Court)).all() == []


def test_each_session_gets_its_own_seed(db):
    a = _make_session(db, "午前")
    b = _make_session(db, "午後")
    assert a.random_seed != b.random_seed


def test_each_session_gets_its_own_token(db):
    """URL に出す識別子。連番だと他の練習会を推測できてしまう。"""
    tokens = {_make_session(db, f"会{i}").token for i in range(10)}
    assert len(tokens) == 10


def test_tokens_avoid_confusable_characters(db):
    """読み上げや手入力で取り違えないよう 0/O・1/l/I を使わない。"""
    from app.models import TOKEN_ALPHABET

    assert not set("01lIoO") & set(TOKEN_ALPHABET)
    assert not set("01lIoO") & set(_make_session(db).token)


def test_enum_columns_round_trip_as_enum(db):
    s = _make_session(db)
    db.add(
        Member(
            session_id=s.id,
            nickname="たろう",
            gender=Gender.MALE,
            level=Level.RACKET_EXPERIENCED,
        )
    )
    db.commit()
    db.expire_all()

    loaded = db.scalars(select(Member)).one()
    assert loaded.gender is Gender.MALE
    assert loaded.level is Level.RACKET_EXPERIENCED
    assert loaded.status is MemberStatus.ACTIVE
    assert loaded.baseline == 0


def test_round_tree_cascade_within_session(db):
    s = _make_session(db)
    members = [
        Member(session_id=s.id, nickname=f"m{i}", gender=Gender.FEMALE, level=Level.PICKLEBALL) for i in range(4)
    ]
    db.add_all(members)
    db.commit()

    rnd = Round(session_id=s.id, status=RoundStatus.PENDING)
    db.add(rnd)
    db.commit()
    match = Match(round_id=rnd.id, court_id=s.courts[0].id)
    db.add(match)
    db.commit()
    db.add_all([MatchSlot(match_id=match.id, team_index=i // 2, member_id=members[i].id) for i in range(4)])
    db.add_all(
        [
            RoundParticipation(
                round_id=rnd.id,
                member_id=m.id,
                state=ParticipationState.PLAYED,
                level=m.level,
            )
            for m in members
        ]
    )
    db.commit()

    assert db.scalar(select(MatchSlot).where(MatchSlot.member_id == members[0].id)) is not None

    # ラウンドを消すと試合・出場枠・スナップショットも消える
    db.delete(rnd)
    db.commit()
    assert db.scalars(select(Match)).all() == []
    assert db.scalars(select(MatchSlot)).all() == []
    assert db.scalars(select(RoundParticipation)).all() == []
    # メンバーは残る
    assert len(db.scalars(select(Member)).all()) == 4


def test_deleting_session_removes_everything_under_it(db):
    s = _make_session(db)
    m = Member(session_id=s.id, nickname="a", gender=Gender.MALE, level=Level.PICKLEBALL)
    db.add(m)
    db.commit()
    rnd = Round(session_id=s.id)
    db.add(rnd)
    db.commit()
    db.add(RoundParticipation(round_id=rnd.id, member_id=m.id, state=ParticipationState.SAT_OUT, level=m.level))
    db.commit()

    db.delete(s)
    db.commit()

    assert db.scalars(select(PracticeSession)).all() == []
    assert db.scalars(select(Member)).all() == []
    assert db.scalars(select(Round)).all() == []
    assert db.scalars(select(RoundParticipation)).all() == []


def test_people_survive_session_deletion(db):
    """メンバー台帳は練習会に属さないので、記録を破棄しても残る。"""
    s = _make_session(db)
    db.add(
        Person(
            owner_id=_owner(db).id,
            nickname="たろう",
            gender=Gender.MALE,
            level=Level.PICKLEBALL,
        )
    )
    db.commit()

    db.delete(s)
    db.commit()

    person = db.scalars(select(Person)).one()
    assert person.nickname == "たろう"
    assert person.gender is Gender.MALE


def test_people_may_share_a_nickname(db):
    """台帳のニックネームは一意にしない。

    ニックネームは識別子ではない（不変則14）。見分けるための番号は
    サービス層が振る。DB で縛ると、同名の人を登録できなくなってしまう。
    """
    owner = _owner(db)
    for gender in (Gender.MALE, Gender.FEMALE):
        db.add(
            Person(
                owner_id=owner.id,
                nickname="かぶり",
                gender=gender,
                level=Level.PICKLEBALL,
            )
        )
    db.commit()
    assert len(db.scalars(select(Person)).all()) == 2


def test_the_import_key_is_unique_within_an_owner(db):
    """取り込み元の識別子は団体の中で一意。

    同じ人を二重に取り込むと、毎ラウンド出場枠を1つ食う幽霊ができる。
    団体をまたいだ重複は縛らない（別の団体が同じ人を持つのは当然）。
    """
    owner = _owner(db)
    other = Owner(name="よその団体")
    db.add(other)
    db.flush()
    for holder in (owner, other):
        db.add(
            Person(
                owner_id=holder.id,
                nickname="同じ人",
                gender=Gender.MALE,
                level=Level.PICKLEBALL,
                external_id="bear:9001",
            )
        )
    db.commit()

    db.add(
        Person(
            owner_id=owner.id,
            nickname="二重",
            gender=Gender.MALE,
            level=Level.PICKLEBALL,
            external_id="bear:9001",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_same_nickname_allowed_twice_in_one_session(db):
    """同名は禁止しない。警告は上位層の責務（CLAUDE.md 不変則14）。"""
    s = _make_session(db)
    db.add_all(
        [
            Member(session_id=s.id, nickname="ゆうき", gender=Gender.MALE, level=Level.PICKLEBALL),
            Member(session_id=s.id, nickname="ゆうき", gender=Gender.FEMALE, level=Level.BEGINNER),
        ]
    )
    db.commit()
    assert len(db.scalars(select(Member)).all()) == 2


def test_participation_is_unique_per_round_and_member(db):
    s = _make_session(db)
    m = Member(session_id=s.id, nickname="a", gender=Gender.MALE, level=Level.PICKLEBALL)
    db.add(m)
    db.commit()
    rnd = Round(session_id=s.id)
    db.add(rnd)
    db.commit()
    db.add(RoundParticipation(round_id=rnd.id, member_id=m.id, state=ParticipationState.PLAYED, level=m.level))
    db.commit()
    db.add(RoundParticipation(round_id=rnd.id, member_id=m.id, state=ParticipationState.SAT_OUT, level=m.level))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
