"""ラウンドの採用・不採用・取り消しと、統計への反映。"""

from __future__ import annotations

import pytest

from app.errors import ConflictError, NotEnoughPlayersError
from app.models import Round
from app.scheduler.domain import Gender, Level, MemberStatus, RoundStatus
from app.services import people as people_service
from app.services import rounds as rounds_service
from app.services import sessions as sessions_service
from app.services import stats as stats_service
from app.services.owners import current_owner


def make_session(db, count: int = 13, court_count: int = 2, *, name: str = "練習会"):
    owner = current_owner(db)
    session = sessions_service.create_session(db, owner, name, court_count)
    for i in range(count):
        person = people_service.add_person(
            db,
            owner,
            nickname=f"m{i + 1}",
            gender=Gender.MALE if i % 2 == 0 else Gender.FEMALE,
            level=Level.PICKLEBALL,
        )
        sessions_service.add_member(db, session, person)
    db.refresh(session)
    return session


def test_adopting_updates_the_statistics(db):
    session = make_session(db)
    round_ = rounds_service.generate(db, session)
    playing = {slot.member_id for m in round_.matches for slot in m.slots}

    assert stats_service.play_counts(db, session.id) == {}

    rounds_service.adopt(db, round_)
    counts = stats_service.play_counts(db, session.id)
    assert {mid for mid, c in counts.items() if c == 1} == playing
    assert len(counts) == 13, "出場しなかった人にも記録が残る"
    assert round_.seq == 1


def test_rejecting_does_not_touch_the_statistics(db):
    """不採用は参加回数に一切反映しない（仕様の要求）。"""
    session = make_session(db)
    first = rounds_service.generate(db, session)
    rounds_service.reject(db, first)

    assert stats_service.play_counts(db, session.id) == {}
    assert len(stats_service.adopted_rounds(db, session.id)) == 0

    second = rounds_service.generate(db, session)
    rounds_service.adopt(db, second)
    assert sum(stats_service.play_counts(db, session.id).values()) == 8


def test_undo_restores_everything(db):
    session = make_session(db)
    rounds_service.adopt(db, rounds_service.generate(db, session))
    before = stats_service.play_counts(db, session.id)
    history_before = stats_service.build_history(db, session.id)

    second = rounds_service.generate(db, session)
    rounds_service.adopt(db, second)
    rounds_service.undo(db, second)

    assert stats_service.play_counts(db, session.id) == before
    assert stats_service.build_history(db, session.id).partner_count == (
        history_before.partner_count
    )


def test_only_the_latest_adopted_round_can_be_undone(db):
    session = make_session(db)
    first = rounds_service.adopt(db, rounds_service.generate(db, session))
    rounds_service.adopt(db, rounds_service.generate(db, session))
    with pytest.raises(ConflictError):
        rounds_service.undo(db, first)


def test_adopting_twice_is_rejected(db):
    """別端末が先に開始していたら、後から押した方は弾く。"""
    session = make_session(db)
    round_ = rounds_service.generate(db, session)
    rounds_service.adopt(db, round_)
    with pytest.raises(ConflictError):
        rounds_service.adopt(db, round_)


def test_generating_again_replaces_the_pending_round(db):
    session = make_session(db)
    first = rounds_service.generate(db, session)
    second = rounds_service.generate(db, session)
    assert first.status is RoundStatus.REJECTED
    assert second.status is RoundStatus.PENDING
    assert second.attempt == 1
    assert rounds_service.current_round(db, session.id).id == second.id


def test_skipping_gives_a_different_card(db):
    session = make_session(db)
    first = rounds_service.generate(db, session)
    first_sig = rounds_service._signature(first)
    second = rounds_service.generate(db, session)
    assert rounds_service._signature(second) != first_sig


def test_member_changes_do_not_touch_the_pending_round(db):
    """メンバーを変えても、表示中のマッチは動かない。反映は次の生成から。"""
    session = make_session(db)
    round_ = rounds_service.generate(db, session)
    before = rounds_service._signature(round_)

    playing = next(iter({s.member_id for m in round_.matches for s in m.slots}))
    sessions_service.update_member(
        db, sessions_service.get_member(db, playing), status=MemberStatus.RESTING
    )

    db.refresh(round_)
    assert rounds_service._signature(round_) == before

    regenerated = rounds_service.generate(db, session)
    assert playing not in {s.member_id for m in regenerated.matches for s in m.slots}


def test_a_late_joiner_gets_a_baseline(db):
    session = make_session(db, count=12)
    for _ in range(4):
        rounds_service.adopt(db, rounds_service.generate(db, session))

    established = min(
        p.adjusted
        for p in stats_service.build_player_stats(db, session.id)
        if p.status is MemberStatus.ACTIVE
    )
    late = sessions_service.add_member(
        db,
        session,
        people_service.add_person(
            db,
            current_owner(db),
            nickname="遅刻",
            gender=Gender.FEMALE,
            level=Level.PICKLEBALL,
        ),
    )
    assert late.baseline == established


# ---------------------------------------------------------------------------
# コートの増減
# ---------------------------------------------------------------------------


def test_taking_a_court_out_of_play_shrinks_the_round(db):
    """1面を練習コートに回すと、次の生成から1面ぶんだけ組まれる。"""
    session = make_session(db, count=13, court_count=2)
    assert len(rounds_service.generate(db, session).matches) == 2

    sessions_service.update_court(db, session, session.courts[1].id, in_use=False)
    db.refresh(session)

    round_ = rounds_service.generate(db, session)
    assert len(round_.matches) == 1
    assert round_.matches[0].court_id == session.courts[0].id


def test_a_court_can_come_back(db):
    session = make_session(db, count=13, court_count=2)
    sessions_service.update_court(db, session, session.courts[1].id, in_use=False)
    db.refresh(session)
    rounds_service.adopt(db, rounds_service.generate(db, session))

    sessions_service.update_court(db, session, session.courts[1].id, in_use=True)
    db.refresh(session)
    assert len(rounds_service.generate(db, session).matches) == 2


def test_matches_land_on_the_courts_that_are_in_use(db):
    """真ん中のコートを外しても、残ったコートに正しく割り当てる。"""
    session = make_session(db, count=13, court_count=3)
    sessions_service.update_court(db, session, session.courts[1].id, in_use=False)
    db.refresh(session)

    round_ = rounds_service.generate(db, session)
    used = {m.court_id for m in round_.matches}
    assert used == {session.courts[0].id, session.courts[2].id}


def test_the_last_court_cannot_be_taken_out(db):
    session = make_session(db, court_count=2)
    sessions_service.update_court(db, session, session.courts[0].id, in_use=False)
    db.refresh(session)
    with pytest.raises(ConflictError):
        sessions_service.update_court(db, session, session.courts[1].id, in_use=False)


def test_members_moved_to_the_practice_court_are_treated_as_resting(db):
    """練習コートに回すメンバーは休憩扱い。試合の出場者には数えない。"""
    session = make_session(db, count=12, court_count=2)
    sessions_service.update_court(db, session, session.courts[1].id, in_use=False)
    db.refresh(session)

    coach = sessions_service.list_members(db, session.id)[0]
    trainee = sessions_service.list_members(db, session.id)[1]
    for member in (coach, trainee):
        sessions_service.update_member(db, member, status=MemberStatus.RESTING)

    round_ = rounds_service.generate(db, session)
    playing = {s.member_id for m in round_.matches for s in m.slots}
    assert not playing & {coach.id, trainee.id}

    rounds_service.adopt(db, round_)
    stat = next(
        p for p in stats_service.build_player_stats(db, session.id) if p.id == coach.id
    )
    assert stat.plays == 0


def test_not_enough_players_for_even_one_court(db):
    session = make_session(db, count=3)
    with pytest.raises(NotEnoughPlayersError):
        rounds_service.generate(db, session)


def _beginner_session(db, beginners: int = 1, count: int = 8):
    """全員ピックルボール経験者のうち、先頭の何人かを初心者にした練習会。"""
    owner = current_owner(db)
    session = sessions_service.create_session(db, owner, "レベル変更", 2)
    members = [
        sessions_service.add_member(
            db,
            session,
            people_service.add_person(
                db,
                owner,
                nickname=f"m{i + 1}",
                gender=Gender.MALE if i % 2 == 0 else Gender.FEMALE,
                level=Level.BEGINNER if i < beginners else Level.PICKLEBALL,
            ),
        )
        for i in range(count)
    ]
    db.refresh(session)
    return session, members


def _partner_of(round_, member_id: int) -> int:
    """その人と同じチームに入ったもう一人。"""
    for match in round_.matches:
        for team_index in (0, 1):
            team = [s.member_id for s in match.slots if s.team_index == team_index]
            if member_id in team:
                return next(x for x in team if x != member_id)
    raise AssertionError("出場していない")


def test_promoting_a_beginner_keeps_the_past_burden(db):
    """初心者を経験者に変えても、過去に受け持った回数は消えない。

    消えると、すでに何度も受け持った人が「まだ受け持っていない」ことになり、
    残りの初心者をまた割り当てられる。当時のレベルで固定する必要がある。
    """
    session, members = _beginner_session(db, beginners=1)
    beginner = members[0]

    round_ = rounds_service.generate(db, session)
    rounds_service.adopt(db, round_)
    partner = _partner_of(round_, beginner.id)
    assert stats_service.build_history(db, session.id).beginner_partner_count[partner] == 1

    sessions_service.update_member(db, beginner, level=Level.PICKLEBALL)

    after = stats_service.build_history(db, session.id).beginner_partner_count
    assert after[partner] == 1, "昇格させても、当時受け持った事実は残る"


def test_demoting_a_player_does_not_backdate_the_burden(db):
    """経験者を初心者に変えても、過去に遡って負担が計上されない。

    計上されると、実際には受け持っていない人が以後は免除されてしまう。
    """
    session, members = _beginner_session(db, beginners=0)

    round_ = rounds_service.generate(db, session)
    rounds_service.adopt(db, round_)
    assert stats_service.build_history(db, session.id).beginner_partner_count == {}

    sessions_service.update_member(db, members[0], level=Level.BEGINNER)

    after = stats_service.build_history(db, session.id).beginner_partner_count
    assert after == {}, "当時は初心者ではないので、誰も受け持っていない"


def test_a_level_change_takes_effect_from_the_next_generation(db):
    """レベルの変更は、次に生成するマッチから効く。"""
    session, members = _beginner_session(db, beginners=0)
    rounds_service.adopt(db, rounds_service.generate(db, session))

    for member in members[:2]:
        sessions_service.update_member(db, member, level=Level.BEGINNER)
    db.refresh(session)

    round_ = rounds_service.generate(db, session)
    assert _partner_of(round_, members[0].id) != members[1].id, (
        "初心者にしたらすぐ、初心者同士のペアが避けられる"
    )


def test_a_level_change_does_not_move_the_displayed_match(db):
    """表示中のマッチは、レベルを変えても動かない（不変則12）。"""
    session, members = _beginner_session(db, beginners=0)
    round_ = rounds_service.generate(db, session)
    before = [
        (s.match_id, s.team_index, s.member_id) for m in round_.matches for s in m.slots
    ]

    for member in members[:2]:
        sessions_service.update_member(db, member, level=Level.BEGINNER)

    db.refresh(round_)
    after = [
        (s.match_id, s.team_index, s.member_id) for m in round_.matches for s in m.slots
    ]
    assert after == before


# ---------------------------------------------------------------------------
# 別端末からの同時操作
#
# 管理画面・全体表示画面・メンバー用画面は別端末から同時に使われる前提。
# 状態を在メモリで見てから書くと、相手のコミットを見落として両方成功する。
# ---------------------------------------------------------------------------


def _two_views(session_factory, round_id: int):
    """同じラウンドを、2つの端末がそれぞれ読んだ状態を作る。"""
    left, right = session_factory(), session_factory()
    return left, right, left.get(Round, round_id), right.get(Round, round_id)


def test_skipping_after_another_device_started_is_rejected(db, session_factory):
    """開始した直後にスキップが押されても、開始した事実は消えない。"""
    session = make_session(db)
    pending = rounds_service.generate(db, session)

    left, right, seen_by_left, seen_by_right = _two_views(session_factory, pending.id)
    rounds_service.adopt(left, seen_by_left)

    # 右の端末は、まだ pending だと思っている。
    assert seen_by_right.status is RoundStatus.PENDING
    with pytest.raises(ConflictError):
        rounds_service.reject(right, seen_by_right)

    assert len(stats_service.adopted_rounds(db, session.id)) == 1
    assert sum(stats_service.play_counts(db, session.id).values()) == 8


def test_starting_the_same_round_twice_from_two_devices(db, session_factory):
    """同じマッチを2台から同時に開始しても、採用は1回だけ。"""
    session = make_session(db)
    pending = rounds_service.generate(db, session)

    left, right, seen_by_left, seen_by_right = _two_views(session_factory, pending.id)
    rounds_service.adopt(left, seen_by_left)
    with pytest.raises(ConflictError):
        rounds_service.adopt(right, seen_by_right)

    assert len(stats_service.adopted_rounds(db, session.id)) == 1


def test_a_leftover_pending_does_not_hide_the_started_match(db, session_factory):
    """pending が2本できても、開始したマッチが隠れない。

    生成が同時に走ると pending が2本できることがある。残したままだと
    表示画面が古い方を出し続け、しかもそれを二重に採用できてしまう。
    """
    session = make_session(db)
    stale = rounds_service.generate(db, session)
    # 生成の競合で、もう1本 pending ができた状況を作る。
    fresh = Round(session_id=session.id, status=RoundStatus.PENDING, attempt=1)
    db.add(fresh)
    db.commit()

    rounds_service.adopt(db, fresh)

    db.refresh(stale)
    assert stale.status is RoundStatus.REJECTED, "古い pending は片付ける"
    current = rounds_service.current_round(db, session.id)
    assert current is not None and current.id == fresh.id, "開始したマッチが出る"
    with pytest.raises(ConflictError):
        rounds_service.adopt(db, stale)


def test_a_member_deleted_while_playing_keeps_the_round_record(db):
    """出場中に削除されたメンバーも、そのラウンドの記録としては残る。

    残さないと MatchSlot だけが残り、当時のレベルが引けなくなる。
    初心者を受け持ったパートナーの負担が履歴から消える（不変則2）。
    """
    session, members = _beginner_session(db, beginners=1)
    beginner = members[0]
    round_ = rounds_service.generate(db, session)
    partner = _partner_of(round_, beginner.id)

    sessions_service.remove_member(db, beginner)
    rounds_service.adopt(db, round_)

    levels = stats_service.round_levels(db, session.id)[round_.id]
    assert levels[beginner.id] is Level.BEGINNER, "当時のレベルが残る"
    history = stats_service.build_history(db, session.id)
    assert history.beginner_partner_count[partner] == 1, "受け持った事実が消えない"
