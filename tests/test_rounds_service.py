"""ラウンドの採用・不採用・取り消しと、統計への反映。"""

from __future__ import annotations

import pytest

from app.errors import ConflictError, NotEnoughPlayersError
from app.scheduler.domain import Gender, Level, MemberStatus, RoundStatus
from app.services import rounds as rounds_service
from app.services import sessions as sessions_service
from app.services import stats as stats_service


def make_session(db, count: int = 13, court_count: int = 2):
    session = sessions_service.create_session(db, "練習会", court_count)
    for i in range(count):
        sessions_service.add_member(
            db,
            session,
            nickname=f"m{i + 1}",
            gender=Gender.MALE if i % 2 == 0 else Gender.FEMALE,
            level=Level.PICKLEBALL,
        )
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
        db, session, nickname="遅刻", gender=Gender.FEMALE, level=Level.PICKLEBALL
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
