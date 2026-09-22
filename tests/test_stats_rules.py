"""スナップショット列からの統計導出。

ここが狂うと公平性の計算がすべて狂うので、導出規則そのものを直接検証する。
"""

from __future__ import annotations

import pytest

from app.scheduler.domain import MemberStatus, ParticipationState
from app.scheduler.stats_rules import (
    count_plays,
    count_rest_credit,
    count_sit_out_streak,
    derive,
    is_just_returned,
)

PLAYED = ParticipationState.PLAYED
SAT_OUT = ParticipationState.SAT_OUT
RESTING = ParticipationState.RESTING


def test_count_plays_counts_only_played():
    assert count_plays([PLAYED, SAT_OUT, RESTING, PLAYED]) == 2
    assert count_plays([]) == 0
    assert count_plays([SAT_OUT, RESTING]) == 0


@pytest.mark.parametrize(
    ("states", "expected", "why"),
    [
        ([], 0, "履歴が無ければクレジットも無い"),
        ([PLAYED, PLAYED], 0, "休んでいなければ 0"),
        ([RESTING], 0, "1試合だけの休みは 1試合分の不参加そのもの"),
        ([RESTING, RESTING], 1, "2試合まとめて休んでも不参加は1試合分"),
        ([RESTING, RESTING, RESTING], 2, "3試合まとめて休んでも不参加は1試合分"),
        ([PLAYED, RESTING, RESTING, PLAYED], 1, "途中の休みブロックも同じ扱い"),
        ([RESTING, PLAYED, RESTING], 0, "別々に1試合ずつ休んだら、それぞれ1試合分の不参加"),
        ([RESTING, RESTING, PLAYED, RESTING, RESTING], 2, "ブロックごとに 長さ-1 を足す"),
        ([RESTING, SAT_OUT, RESTING], 0, "出番なしを挟むとブロックは切れる"),
    ],
)
def test_count_rest_credit(states, expected, why):
    """連続した休みブロック1つにつき「長さ-1」をみなし出場として加算する。

    仕様「2試合分以上固めて休んだ場合でも、1試合分の不参加という扱いでよい」の実装。
    休んだ分を後で取り返させると、また疲れて休むことになる、というのが仕様の意図。
    """
    assert count_rest_credit(states) == expected, why


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([], 0),
        ([PLAYED], 0),
        ([SAT_OUT], 1),
        ([SAT_OUT, SAT_OUT, SAT_OUT], 3),
        ([SAT_OUT, PLAYED], 0),
        ([PLAYED, SAT_OUT, SAT_OUT], 2),
        ([SAT_OUT, SAT_OUT, RESTING], 0),
    ],
)
def test_count_sit_out_streak(states, expected):
    """末尾から続く「出場可能だったのに出番がなかった」回数だけを数える。"""
    assert count_sit_out_streak(states) == expected


def test_resting_does_not_count_as_sit_out_streak():
    """自分の意思で休んでいる間は、連続不参加としては数えない。"""
    assert count_sit_out_streak([RESTING, RESTING, RESTING]) == 0


@pytest.mark.parametrize(
    ("states", "status", "expected"),
    [
        ([RESTING], MemberStatus.ACTIVE, True),
        ([PLAYED, RESTING, RESTING], MemberStatus.ACTIVE, True),
        ([RESTING, PLAYED], MemberStatus.ACTIVE, False),
        ([RESTING, SAT_OUT], MemberStatus.ACTIVE, False),
        ([], MemberStatus.ACTIVE, False),
        ([RESTING], MemberStatus.RESTING, False),
        ([RESTING], MemberStatus.LEFT, False),
    ],
)
def test_is_just_returned(states, status, expected):
    """休憩から復帰した直後で、まだ出場していないときだけ True。"""
    assert is_just_returned(states, status) is expected


def test_derive_combines_all_rules():
    states = [PLAYED, SAT_OUT, RESTING, RESTING, RESTING]
    result = derive(states, MemberStatus.ACTIVE)
    assert result.plays == 1
    assert result.rest_credit == 2
    assert result.sit_out_streak == 0
    assert result.just_returned is True


def test_long_rest_costs_only_one_match_of_deficit():
    """3ラウンド続けて休んだ人の欠損は、休まなかった人に対してちょうど1試合分。"""
    stayed = derive([PLAYED, PLAYED, PLAYED], MemberStatus.ACTIVE)
    rested = derive([RESTING, RESTING, RESTING], MemberStatus.ACTIVE)
    stayed_adjusted = stayed.plays + stayed.rest_credit
    rested_adjusted = rested.plays + rested.rest_credit
    assert stayed_adjusted - rested_adjusted == 1
