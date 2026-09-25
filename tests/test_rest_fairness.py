"""休憩・遅刻・離脱の公平さ。人の感覚の3原則（doc/spec.md 冒頭の要求）を性質として確かめる。

1. 2試合以上の長い休憩から戻った人は、試合に戻す。休んだ分を取り返させはしない。
2. 几帳面に宣言して短く抜けた人は、損も得もしない。
3. 遅刻した人は、参加した時点からの公平さが守られていればよい（特別扱いはしない）。

出場の割合が構成によって違う（16名2面は1ラウンドに半分、16名4面は全員）ので、
いくつかの構成で見る。
"""

from __future__ import annotations

import math
import statistics

import pytest

from app.models import Person
from app.scheduler.domain import Gender, Level, MemberStatus
from app.services import people as people_service
from app.services import rounds as rounds_service
from app.services import sessions as sessions_service
from app.services import stats as stats_service
from app.services.owners import current_owner
from tests.simulation import MemberSpec, Simulator, make_members

# (人数, 面数)。出番待ちが多い → 少ない → 全員が毎回出る。
CONFIGS = [(12, 1), (16, 2), (13, 2), (13, 3), (12, 3), (16, 4), (8, 2)]
SEEDS = [1, 2, 3]
WATCHED = 1
"""観察する人の member_id。"""


def _usual_interval(count: int, courts: int) -> int:
    """普段の出場の間隔（ラウンド数）。1ラウンドに出る割合の逆数を切り上げたもの。

    例: 12名1面は3人に1人が出るので3（2試合休んで1回出るのが普段どおり）。
    """
    return math.ceil(count / (courts * 4))


def _long_rest(count: int, courts: int) -> int:
    """これ以上休んだら「長い休憩」とみなすラウンド数。

    普段の出番待ちより長く休んだときに初めて、休み明けとしてすぐ出すのが公平になる。
    仕様の「2試合以上」も下限にする。
    """
    return max(2, _usual_interval(count, courts))


LONG_RESTS = [
    (count, courts, rest)
    for count, courts in CONFIGS
    for rest in sorted({_long_rest(count, courts), _long_rest(count, courts) + 2, 6})
]


def _plays_in(plans, member_id: int) -> int:
    return sum(member_id in plan.playing for plan in plans)


def _median_of_others(plans, member_ids, watched: int) -> float:
    return statistics.median(_plays_in(plans, i) for i in member_ids if i != watched)


# ---------------------------------------------------------------------------
# 原則1: 長い休憩から戻った人は、試合に戻す
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize(("count", "courts", "rest"), LONG_RESTS)
def test_someone_back_from_a_long_rest_plays_right_away(count, courts, rest, seed):
    """普段の出番待ちより長く休んで戻った人は、戻った直後のラウンドに出場する。

    「長い休憩」は `_long_rest`（普段の出場の間隔と、仕様の2試合の大きい方）以上。
    皆が2試合休んで1回出る構成なら、3試合以上休んだときに初めて「すぐ出す」のが公平。
    """
    sim = Simulator(make_members(count), seed=seed, court_count=courts)
    sim.run(6)
    sim.set_status(WATCHED, MemberStatus.RESTING)
    sim.run(rest)
    sim.set_status(WATCHED, MemberStatus.ACTIVE)
    (first,) = sim.run(1)
    assert WATCHED in first.playing, f"{count}名{courts}面 {rest}R休憩: 戻った直後に出ていない"


def _gap_to_others(sim, member_id: int):
    """その人の adjusted と、他の人の adjusted の平均との差（端数を含む）。"""
    stats = {p.id: p.adjusted for p in sim.player_stats()}
    others = [v for k, v in stats.items() if k != member_id]
    return stats[member_id] - sum(others) / len(others)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize(("count", "courts", "rest"), LONG_RESTS)
def test_a_long_rest_costs_at_most_one_match(count, courts, rest, seed):
    """休んだ分を取り返させない。休憩で他の人との差が開くのは、1試合分まで。

    仕様「2試合分以上固めて休んだ場合でも、1試合分の不参加という扱いでよい」。
    差が1試合分を超えて開くと、戻った後にその分を取り戻すように出場させてしまう。
    出場回数を数える窓で確かめると、出場の割合が低い構成では出番の巡り合わせで
    ぶれるので、adjusted（端数を含む）で直接確かめる。
    """
    sim = Simulator(make_members(count), seed=seed, court_count=courts)
    sim.run(6)
    before = _gap_to_others(sim, WATCHED)
    sim.set_status(WATCHED, MemberStatus.RESTING)
    sim.run(rest)
    sim.set_status(WATCHED, MemberStatus.ACTIVE)
    assert before - _gap_to_others(sim, WATCHED) <= 1


# ---------------------------------------------------------------------------
# 原則2: 宣言して短く抜けた人は、損も得もしない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize(("count", "courts"), CONFIGS)
def test_a_short_declared_absence_is_neither_a_loss_nor_a_gain(count, courts, seed):
    """几帳面に宣言して1ラウンドだけ抜けた人は、損も得もしない。

    - 損しない: 戻ってから普段の出場の間隔以内に出場する（出番待ちの人と同じ扱いで、後回しにしない）
    - 得しない: 抜けた分を取り戻すような連続出場をしない

    抜けた試合そのものは取り戻させない（取り戻させると悪循環になる。doc/spec.md 冒頭）。
    休憩は何度でも取れるので、3回に分けて抜ける。
    """
    usual = _usual_interval(count, courts)
    sim = Simulator(make_members(count), seed=seed, court_count=courts)
    sim.run(4)
    for _ in range(3):
        sim.set_status(WATCHED, MemberStatus.RESTING)
        sim.run(1)
        sim.set_status(WATCHED, MemberStatus.ACTIVE)
        back = sim.run(usual)
        assert _plays_in(back, WATCHED) >= 1, (
            f"{count}名{courts}面: 1ラウンド抜けて戻った後、普段の間隔（{usual}R）で出ていない"
        )
        sim.run(3)
    after = sim.run(8)
    median = _median_of_others(after, range(1, count + 1), WATCHED)
    assert _plays_in(after, WATCHED) <= median + 1, "抜けた分を取り戻させている"


def _session(db, count: int = 9, courts: int = 2):
    owner = current_owner(db)
    session = sessions_service.create_session(db, owner, "練習会", courts)
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


def _playing(round_) -> set[int]:
    return {slot.member_id for match in round_.matches for slot in match.slots}


def _next(db, session):
    round_ = rounds_service.generate(db, session)
    rounds_service.adopt(db, round_)
    return round_


def _stat(db, session, member_id: int):
    return next(p for p in stats_service.build_player_stats(db, session.id) if p.id == member_id)


def _member(db, session, nickname: str = "m1"):
    return next(m for m in sessions_service.list_members(db, session.id) if m.nickname == nickname)


def test_a_rest_undone_before_the_next_card_leaves_no_trace(db):
    """試合の最中に休憩にして、次のカードを作る前に戻れば、統計は何も変わらない。"""
    session = _session(db)
    _next(db, session)
    _next(db, session)
    member = _member(db, session)
    before = _stat(db, session, member.id)

    sessions_service.update_member(db, member, status=MemberStatus.RESTING)
    sessions_service.update_member(db, member, status=MemberStatus.ACTIVE)

    assert _stat(db, session, member.id) == before


def test_back_before_the_start_counts_as_sitting_out_and_plays_next(db):
    """休憩中に次のカードが作られ、「開始」の前に戻った。

    そのカードには入らないが、休憩ではなく「出番なし」として扱い、次のカードで出す。
    """
    session = _session(db)
    _next(db, session)
    _next(db, session)
    member = _member(db, session)

    sessions_service.update_member(db, member, status=MemberStatus.RESTING)
    pending = rounds_service.generate(db, session)
    assert member.id not in _playing(pending), "休憩中の人がカードに入っている"
    sessions_service.update_member(db, member, status=MemberStatus.ACTIVE)
    rounds_service.adopt(db, pending)

    stat = _stat(db, session, member.id)
    assert stat.sit_out_streak >= 1, "出番なしとして数えられていない"
    assert stat.rest_credit == 0
    assert member.id in _playing(_next(db, session)), "次のカードで出ていない"


# ---------------------------------------------------------------------------
# 原則3: 遅刻した人は、参加した時点からの公平さ
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize(("count", "courts"), CONFIGS)
def test_a_late_joiner_plays_at_the_same_pace_from_joining(count, courts, seed):
    """遅刻した人の、参加してからの出場回数は、同じ期間の他の人の中央値 ±1。"""
    sim = Simulator(make_members(count), seed=seed, court_count=courts)
    sim.run(6)
    late = count + 1
    sim.add_member(MemberSpec(id=late, nickname=f"m{late}"))
    after = sim.run(12)
    median = _median_of_others(after, range(1, late + 1), late)
    assert abs(_plays_in(after, late) - median) <= 1


# ---------------------------------------------------------------------------
# 離脱から戻った人（途中参加と同じ扱い。ユーザー判断）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rests_before_leaving", [0, 1, 2])
def test_someone_who_left_and_came_back_restarts_at_the_bottom(db, rests_before_leaving):
    """離脱から戻った人は、戻った時点で出場可能メンバーの最小 adjusted から再開し、すぐ出る。

    離脱前の出場回数や休憩の不足は、戻った時点で帳消しにする（途中参加と同じ）。
    """
    session = _session(db)
    for _ in range(3):
        _next(db, session)
    member = _member(db, session)
    for _ in range(rests_before_leaving):
        sessions_service.update_member(db, member, status=MemberStatus.RESTING)
        _next(db, session)
        sessions_service.update_member(db, member, status=MemberStatus.ACTIVE)
        _next(db, session)

    sessions_service.remove_member(db, member)
    for _ in range(4):
        _next(db, session)
    back = sessions_service.add_member(db, session, db.get(Person, member.person_id))

    stats = stats_service.build_player_stats(db, session.id)
    lowest = min(p.adjusted for p in stats if p.status is MemberStatus.ACTIVE and p.id != back.id)
    assert _stat(db, session, back.id).adjusted == lowest
    soon = _playing(_next(db, session)) | _playing(_next(db, session))
    assert back.id in soon, "戻っても出番が無い"


def test_rests_on_both_sides_of_leaving_are_counted_separately(db):
    """「休憩 → 離脱 → 戻ってすぐ休憩」は2回の休憩。記録の上でつながって1回にならない。

    離脱中は記録が書かれないので、並べると休憩が隣り合ってしまう。
    """
    session = _session(db)
    for _ in range(3):
        _next(db, session)
    member = _member(db, session)
    sessions_service.update_member(db, member, status=MemberStatus.RESTING)
    _next(db, session)
    sessions_service.remove_member(db, member)
    _next(db, session)
    _next(db, session)
    back = sessions_service.add_member(db, session, db.get(Person, member.person_id))
    sessions_service.update_member(db, back, status=MemberStatus.RESTING)
    _next(db, session)

    credit = _stat(db, session, back.id).rest_credit
    assert credit == 0, "2回の休憩が1つのまとまりとして数えられている"
