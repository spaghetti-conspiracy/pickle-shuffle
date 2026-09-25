"""DB の記録から、生成ロジックが使う統計を組み立てる。

参加回数などは members テーブルに持たず、採用ラウンドのスナップショットから
毎回導出する（CLAUDE.md 不変則2）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Match, MatchSlot, Member, Round, RoundParticipation
from app.scheduler.domain import (
    History,
    Level,
    MemberStatus,
    ParticipationState,
    PlayerStat,
    RoundStatus,
    pair_key,
)
from app.scheduler.stats_rules import derive


def adopted_rounds(db: Session, session_id: int) -> list[Round]:
    """採用済みラウンドを採用順に返す。"""
    return list(
        db.scalars(
            select(Round)
            .where(Round.session_id == session_id, Round.status == RoundStatus.ADOPTED)
            .order_by(Round.seq)
        )
    )


def participation_states(db: Session, session_id: int) -> dict[int, list[ParticipationState]]:
    """メンバーごとの状態スナップショットを、採用順に並べて返す。"""
    rows = db.execute(
        select(RoundParticipation.member_id, RoundParticipation.state, Round.seq)
        .join(Round, Round.id == RoundParticipation.round_id)
        .where(Round.session_id == session_id, Round.status == RoundStatus.ADOPTED)
        .order_by(Round.seq)
    ).all()

    states: dict[int, list[ParticipationState]] = {}
    for member_id, state, _seq in rows:
        states.setdefault(member_id, []).append(state)
    return states


def participation_records(
    db: Session, session_id: int
) -> dict[int, list[tuple[int, ParticipationState]]]:
    """メンバーごとの (ラウンドの通し番号, 状態) を、採用順に並べて返す。

    番号が飛んでいるところは、その人が離脱していて記録が無い期間。
    """
    rows = db.execute(
        select(RoundParticipation.member_id, RoundParticipation.state, Round.seq)
        .join(Round, Round.id == RoundParticipation.round_id)
        .where(Round.session_id == session_id, Round.status == RoundStatus.ADOPTED)
        .order_by(Round.seq)
    ).all()

    records: dict[int, list[tuple[int, ParticipationState]]] = {}
    for member_id, state, seq in rows:
        records.setdefault(member_id, []).append((seq, state))
    return records


def round_levels(db: Session, session_id: int) -> dict[int, dict[int, Level]]:
    """ラウンドごとの、そのとき記録されたレベルを返す。

    現在のレベルではなくスナップショットを使う。途中でレベルを変えても
    過去の履歴が書き換わらないようにするため（CLAUDE.md 不変則2）。
    """
    rows = db.execute(
        select(RoundParticipation.round_id, RoundParticipation.member_id, RoundParticipation.level)
        .join(Round, Round.id == RoundParticipation.round_id)
        .where(Round.session_id == session_id, Round.status == RoundStatus.ADOPTED)
    ).all()

    levels: dict[int, dict[int, Level]] = {}
    for round_id, member_id, level in rows:
        levels.setdefault(round_id, {})[member_id] = level
    return levels


def build_player_stats(db: Session, session_id: int) -> list[PlayerStat]:
    """生成に渡す PlayerStat の一覧。離脱済みのメンバーは含めない。"""
    members = list(
        db.scalars(
            select(Member)
            .where(Member.session_id == session_id, Member.status != MemberStatus.LEFT)
            .order_by(Member.id)
        )
    )
    records = participation_records(db, session_id)

    stats = []
    for member in members:
        own = records.get(member.id, [])
        derived = derive(
            [state for _seq, state in own], member.status, seqs=[seq for seq, _state in own]
        )
        stats.append(
            PlayerStat(
                id=member.id,
                nickname=member.nickname,
                gender=member.gender,
                level=member.level,
                baseline=member.baseline,
                plays=derived.plays,
                rest_credit=derived.rest_credit,
                sit_out_streak=derived.sit_out_streak,
                just_returned=derived.just_returned,
                status=member.status,
            )
        )
    return stats


def build_history(db: Session, session_id: int) -> History:
    """採用済みラウンドから、ペアと対戦の履歴を積み上げる。"""
    levels = round_levels(db, session_id)

    rows = db.execute(
        select(Round.seq, Match.round_id, Match.id, MatchSlot.team_index, MatchSlot.member_id)
        .join(MatchSlot, MatchSlot.match_id == Match.id)
        .join(Round, Round.id == Match.round_id)
        .where(Round.session_id == session_id, Round.status == RoundStatus.ADOPTED)
        .order_by(Match.id, MatchSlot.team_index, MatchSlot.id)
    ).all()

    teams: dict[int, dict[int, list[int]]] = {}
    round_of: dict[int, int] = {}
    seq_of: dict[int, int | None] = {}
    for seq, round_id, match_id, team_index, member_id in rows:
        round_of[match_id] = round_id
        seq_of[match_id] = seq
        teams.setdefault(match_id, {}).setdefault(team_index, []).append(member_id)
    # 採用済みなら seq は必ず振られている。万一欠けた行があっても直前の候補にはしない。
    latest_seq = max((seq for seq in seq_of.values() if seq is not None), default=None)
    last_round_groups: list[tuple[int, int, int, int]] = []

    history = History()
    for match_id, sides in teams.items():
        team_a = sides.get(0, [])
        team_b = sides.get(1, [])
        if len(team_a) != 2 or len(team_b) != 2:
            continue
        # そのラウンド時点で誰が初心者だったか。
        at_the_time = levels.get(round_of[match_id], {})
        for team in (team_a, team_b):
            key = pair_key(*team)
            history.partner_count[key] = history.partner_count.get(key, 0) + 1
            first, second = team
            was_beginner = {
                m: (m in at_the_time and at_the_time[m].is_beginner) for m in team
            }
            if was_beginner[first] != was_beginner[second]:
                non_beginner = second if was_beginner[first] else first
                history.beginner_partner_count[non_beginner] = (
                    history.beginner_partner_count.get(non_beginner, 0) + 1
                )
        for x in team_a:
            for y in team_b:
                key = pair_key(x, y)
                history.opponent_count[key] = history.opponent_count.get(key, 0) + 1
        group = tuple(sorted((*team_a, *team_b)))
        history.group_count[group] = history.group_count.get(group, 0) + 1
        if latest_seq is not None and seq_of[match_id] == latest_seq:
            last_round_groups.append(group)

    history.last_round_groups = tuple(last_round_groups)
    return history


def play_counts(db: Session, session_id: int) -> dict[int, int]:
    """メンバーごとの実際の出場回数。管理画面の確認用。"""
    states = participation_states(db, session_id)
    return {
        member_id: sum(1 for s in st if s is ParticipationState.PLAYED)
        for member_id, st in states.items()
    }
