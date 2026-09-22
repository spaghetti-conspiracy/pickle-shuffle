"""採用ラウンドのスナップショット列から統計を導出する純粋関数。

参加回数などをメンバー行に累積更新せず、ここで毎回計算する（CLAUDE.md 不変則2）。
DB を知らないので、導出規則そのものを直接テストできる。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.scheduler.domain import MemberStatus, ParticipationState


@dataclass(frozen=True)
class DerivedStats:
    """スナップショット列から導出した統計。"""

    plays: int
    rest_credit: int
    sit_out_streak: int
    just_returned: bool


def count_plays(states: Sequence[ParticipationState]) -> int:
    """実際に出場した回数。"""
    return sum(1 for s in states if s is ParticipationState.PLAYED)


def count_rest_credit(states: Sequence[ParticipationState]) -> int:
    """休憩によるみなし出場回数。

    仕様「2試合分以上まとめて休んだ場合でも、1試合分の不参加という扱いでよい」の実装。
    連続した休憩ブロックの長さを L とすると、L-1 をみなし出場として加算する。
    こうすると、どれだけ長く休んでも参加回数の欠損はブロックあたり 1 に留まる。

    休んだ分を後で取り返させると、また疲れて休むことになる、というのが仕様の意図。
    """
    credit = 0
    in_block = False
    for state in states:
        if state is ParticipationState.RESTING:
            if in_block:
                credit += 1
            else:
                in_block = True
        else:
            in_block = False
    return credit


def count_sit_out_streak(states: Sequence[ParticipationState]) -> int:
    """末尾から続く「出場可能だったのに出番がなかった」回数。

    休憩は自分の意思なのでここには数えない。末尾が休憩なら 0 を返す。
    """
    streak = 0
    for state in reversed(states):
        if state is ParticipationState.SAT_OUT:
            streak += 1
        else:
            break
    return streak


def is_just_returned(states: Sequence[ParticipationState], status: MemberStatus) -> bool:
    """休憩から復帰した直後で、まだ1度も出場していないか。

    仕様の優先度7（復帰したらなるべく早くマッチに入れる）の判定に使う。
    """
    if status is not MemberStatus.ACTIVE:
        return False
    return bool(states) and states[-1] is ParticipationState.RESTING


def derive(states: Sequence[ParticipationState], status: MemberStatus) -> DerivedStats:
    """スナップショット列（採用された順）から統計をまとめて導出する。"""
    return DerivedStats(
        plays=count_plays(states),
        rest_credit=count_rest_credit(states),
        sit_out_streak=count_sit_out_streak(states),
        just_returned=is_just_returned(states, status),
    )
