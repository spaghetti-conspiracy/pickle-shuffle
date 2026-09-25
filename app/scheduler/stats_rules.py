"""採用ラウンドのスナップショット列から統計を導出する純粋関数。

参加回数などをメンバー行に累積更新せず、ここで毎回計算する（CLAUDE.md 不変則2）。
DB を知らないので、導出規則そのものを直接テストできる。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from app.scheduler.domain import MemberStatus, ParticipationState


@dataclass(frozen=True)
class DerivedStats:
    """スナップショット列から導出した統計。"""

    plays: int
    rest_credit: Fraction
    sit_out_streak: int
    just_returned: bool


def count_plays(states: Sequence[ParticipationState]) -> int:
    """実際に出場した回数。"""
    return sum(1 for s in states if s is ParticipationState.PLAYED)


def count_rest_credit(
    states: Sequence[ParticipationState],
    *,
    seqs: Sequence[int] | None = None,
    rates: Sequence[Fraction] | None = None,
) -> Fraction:
    """休憩によるみなし出場回数（端数を含む分数）。

    仕様「2試合分以上まとめて休んだ場合でも、1試合分の不参加という扱いでよい」の実装。
    休憩のまとまりごとに、**休んでいる間に他の人が増やした出場回数の見込み − 1** を
    みなし出場として加算する（0 未満にはしない）。こうすると、どれだけ長く休んでも、
    他の人との差はまとまりあたり 1 試合分に留まる。

    見込みは、休んでいた各ラウンドの「出場の割合」（``rates``: そのラウンドに出場した人数 /
    出場可能で休んでいない人数）の合計。全員が毎ラウンド出る構成では割合が 1 なので、
    まとまりの長さを L として L − 1 になる。出番待ちが多い構成では割合が小さいので、
    L − 1 だと過大で、戻った人が「休まなかった人より出ている」扱いになって後回しにされる
    （16名2面で6ラウンド休むと、戻ってから4ラウンド目まで出番が無かった）。
    割合で割り引くと、普段の出番待ちより長く休んだ人だけが、戻った直後に出る。
    **端数は丸めない。** 丸めると、休憩での不足が1試合分を超えて開き（切り捨て）、
    戻った人に取り戻させてしまう。生成は adjusted の整数部分で出場者を選ぶ
    （`PlayerStat.adjusted_whole`）ので、スコアは整数のまま保てる。
    ``rates`` を省略したときは、割合を 1 とみなす（全員が毎ラウンド出る構成と同じ）。

    休んだ分を後で取り返させると、また疲れて休むことになる、というのが仕様の意図。

    ``seqs`` は各記録のラウンドの通し番号。番号が飛んでいるところ（離脱していて記録が
    無い期間）では休憩のまとまりを切る。切らないと、「休憩 → 離脱 → 戻ってすぐ休憩」の
    2回の休憩が、記録の上で隣り合って1つのまとまりとして数えられてしまう。
    省略したときは、記録が途切れなく続いているものとみなす。
    """
    credit = Fraction(0)
    expected: Fraction | None = None  # 今のまとまりで、他の人が増やした出場回数の見込み
    previous_seq: int | None = None

    def close_block() -> Fraction:
        return max(Fraction(0), expected - 1) if expected is not None else Fraction(0)

    for index, state in enumerate(states):
        seq = seqs[index] if seqs is not None else None
        if seq is not None and previous_seq is not None and seq != previous_seq + 1:
            credit += close_block()
            expected = None
        previous_seq = seq
        if state is ParticipationState.RESTING:
            rate = rates[index] if rates is not None else Fraction(1)
            expected = rate if expected is None else expected + rate
        else:
            credit += close_block()
            expected = None
    return credit + close_block()


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


def derive(
    states: Sequence[ParticipationState],
    status: MemberStatus,
    *,
    seqs: Sequence[int] | None = None,
    rates: Sequence[Fraction] | None = None,
) -> DerivedStats:
    """スナップショット列（採用された順）から統計をまとめて導出する。

    ``seqs`` は各記録のラウンドの通し番号、``rates`` は各記録のラウンドの出場の割合
    （どちらも `count_rest_credit` を参照）。
    """
    return DerivedStats(
        plays=count_plays(states),
        rest_credit=count_rest_credit(states, seqs=seqs, rates=rates),
        sit_out_streak=count_sit_out_streak(states),
        just_returned=is_just_returned(states, status),
    )
