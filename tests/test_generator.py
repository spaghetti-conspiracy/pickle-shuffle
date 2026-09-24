"""対戦カード生成の性質テスト。

個別の出力を固定値で照合すると実装のスナップショットを正解にしてしまうので、
doc/spec.md の要求を性質として検証する（CLAUDE.md「テストを弱体化させない規則」）。
"""

from __future__ import annotations

import dataclasses
import math
import random
from collections import Counter

import pytest

from app.errors import NotEnoughPlayersError
from app.scheduler.domain import (
    Gender,
    History,
    Level,
    MemberStatus,
    PairKind,
    PlayerStat,
    RoundPlan,
    Weights,
)
from app.scheduler.generator import (
    PAIRINGS_OF_FOUR,
    _pair_kind,
    gender_cost,
    generate_round,
    make_rng,
    split_into_matches,
    tie_key,
)
from tests.simulation import MemberSpec, Simulator, make_members

SLACK = 0
"""テストで使う fairness_slack。既定は 0（厳密公平）で、参加回数差の許容値は 1 になる。"""


def player(member_id: int, **kwargs) -> PlayerStat:
    """テスト用の PlayerStat。"""
    defaults = {
        "nickname": f"m{member_id}",
        "gender": Gender.MALE,
        "level": Level.PICKLEBALL,
    }
    defaults.update(kwargs)
    return PlayerStat(id=member_id, **defaults)


def _make_state(stats):
    from app.scheduler.generator import _State

    return _State.from_history(stats, History())


def _make_scorer(stats, state):
    from app.scheduler.domain import Weights
    from app.scheduler.generator import _Scorer

    return _Scorer(stats, state, Weights(), rng=random.Random(0))

def uniform_players(count: int, start_id: int = 1) -> list[PlayerStat]:
    """属性がすべて同じメンバー。編成のスコアが全通り同点になる。"""
    return [player(start_id + i) for i in range(count)]


def max_partner_repeats_allowed(n_members: int, rounds: int, slots: int) -> int:
    """ペア重複回数の許容上限。

    1ラウンドで作られるペアは slots/2 組。全期間の総ペア数を、作りうる相異なる
    ペアの数で割った値が理想的な平均で、そこに 1 の余裕を見る。
    実装がペア重複を均していれば、この範囲に収まるはず。
    """
    total_pairings = rounds * (slots // 2)
    distinct_pairs = math.comb(n_members, 2)
    return math.ceil(total_pairings / distinct_pairs) + 1


def _variety_excess(counts: dict, total_events: int, n_members: int) -> float:
    """重複回数の二乗和が、理論最小値をどれだけ超過しているか。

    同じ総数を、作りうるペアすべてに均等に配分したときの二乗和が最小値になる。
    """
    distinct = math.comb(n_members, 2)
    q, r = divmod(total_events, distinct)
    ideal = r * (q + 1) ** 2 + (distinct - r) * q * q
    return (sum(c * c for c in counts.values()) - ideal) / ideal


def assert_plan_is_consistent(plan: RoundPlan, court_count: int) -> None:
    """どんな入力でも必ず成り立つ不変条件。"""
    assert 1 <= plan.used_court_count <= court_count
    assert plan.court_count == court_count
    assert len(plan.playing) == plan.used_court_count * 4
    assert len(set(plan.playing)) == len(plan.playing), "同じ人が2箇所に出ている"
    assert [m.court_index for m in plan.matches] == list(range(plan.used_court_count)), (
        "使うコートは 0 から順に詰める"
    )
    assert plan.unused_court_indexes == tuple(range(plan.used_court_count, court_count))
    assert not (set(plan.playing) & set(plan.sitting_out))
    assert not (set(plan.playing) & set(plan.resting))
    for match in plan.matches:
        assert len(set(match.member_ids)) == 4


# ---------------------------------------------------------------------------
# 公平性（優先度2）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "rounds"),
    [(8, 24), (9, 24), (10, 24), (12, 24), (13, 26), (16, 24)],
)
def test_play_counts_stay_within_the_fairness_envelope(count, rounds):
    """参加回数の差は、公平性の枠 (slack + 1) を超えない。

    ラウンド数は実運用の規模（1試合7分・最大3時間 = 25ラウンド程度）に合わせている。
    """
    sim = Simulator(make_members(count), seed=1000 + count, fairness_slack=SLACK)
    sim.run(rounds)
    counts = sim.play_counts()
    assert max(counts.values()) - min(counts.values()) <= SLACK + 1


def test_eight_members_on_two_courts_everyone_plays_every_round():
    """8名2面はちょうど全員が出場する。枠を広げても誰かを外してはいけない。"""
    sim = Simulator(make_members(8), seed=4242, fairness_slack=SLACK)
    plans = sim.run(20)
    for plan in plans:
        assert plan.used_court_count == 2
        assert len(plan.sitting_out) == 0
    counts = sim.play_counts()
    assert max(counts.values()) == min(counts.values()) == 20


@pytest.mark.parametrize(
    ("count", "rounds"), [(5, 5), (6, 3), (7, 7), (9, 9), (10, 5), (12, 3), (16, 2)]
)
def test_play_counts_are_exactly_equal_after_a_full_cycle(count, rounds):
    """出場枠がちょうど一巡するラウンド数を回したら、参加回数は完全に揃う。

    既定は厳密公平（fairness_slack=0）なので、出場枠がちょうど割り切れる
    ラウンド数を回せば、参加回数はぴったり同じになる。
    """
    sim = Simulator(make_members(count), seed=909 + count)
    plans = sim.run(rounds)
    slots = plans[0].used_court_count * 4
    assert rounds * slots % count == 0, "テストの前提: ちょうど一巡する組合せを選ぶこと"
    counts = sim.play_counts()
    assert max(counts.values()) == min(counts.values()) == rounds * slots // count


def test_fewer_plays_are_preferred_when_the_envelope_is_widened():
    """公平性の枠を広げたときも、出場回数の少ない人を優先する（優先度2）。

    枠(slack)は「不公平がここまでなら許す」という上限にすぎない。
    その中での選択は、参加回数の少ない人が先に出るのでなければならない。
    履歴を空にして属性も揃えると、出場者の選び方を決めるのは公平性の項だけになる。
    """
    behind = [player(i) for i in range(1, 9)]
    ahead = [player(9, plays=1), player(10, plays=1)]  # 枠を広げると候補に入る
    players = behind + ahead

    for seed in range(5):
        plan = generate_round(
            players,
            History(),
            court_count=2,
            seed=seed,
            rng=make_rng(seed, 1, 0),
            fairness_slack=1,
        )
        assert set(plan.playing) == {p.id for p in behind}, (
            "出場回数が1多い人を、少ない人より先に出している"
        )


def test_a_long_rest_costs_only_one_match_of_deficit():
    """まとめて休んでも不参加は1試合分。休んだ分を取り返させない。

    9名2面にしてあるのは、1名が抜けると残り8名がちょうど2面に収まり、
    休んでいる間は他の全員が毎ラウンド出場するため。こうすると
    出場者のローテーションによる揺れが入らず、休憩ぶんの欠損だけを見られる。
    """
    sim = Simulator(make_members(9), seed=31337)
    sim.run(2)
    resting_id = sim.adopted_plans[-1].playing[0]
    before = sim.stat(resting_id).adjusted

    others_before = {p.id: p.adjusted for p in sim.player_stats() if p.id != resting_id}

    sim.set_status(resting_id, MemberStatus.RESTING)
    sim.run(3)
    sim.set_status(resting_id, MemberStatus.ACTIVE)

    rested = sim.stat(resting_id)
    assert rested.plays == before, "休んでいる間は出場していない"
    assert rested.rest_credit == 2, "3ラウンドの休みブロックは 3-1=2 のみなし出場"
    assert rested.adjusted == before + 2

    others = [p for p in sim.player_stats() if p.id != resting_id]
    gained = [p.adjusted - others_before[p.id] for p in others]
    assert min(gained) == max(gained) == 3, "他の8名は3ラウンドとも出場している"
    assert max(p.adjusted for p in others) - rested.adjusted == 1, "欠損はちょうど1試合分"


def test_a_long_rest_costs_one_match_even_when_slots_are_spare():
    """出場枠が余る人数でも、固め休みの欠損は1試合分のまま。

    10名2面だと毎ラウンド2人が余るので、通常のローテーションによる揺れが
    休憩ぶんの欠損に重なる。両者は別物なので分けて確かめる。
    （このケースは以前 9名に差し替えられて失われていた。）
    """
    sim = Simulator(make_members(10), seed=31337)
    sim.run(2)
    resting_id = sim.adopted_plans[-1].playing[0]
    before = sim.stat(resting_id).adjusted

    sim.set_status(resting_id, MemberStatus.RESTING)
    sim.run(3)
    sim.set_status(resting_id, MemberStatus.ACTIVE)

    rested = sim.stat(resting_id)
    assert rested.plays == before, "休んでいる間は出場していない"
    assert rested.rest_credit == 2, "3ラウンドの休みブロックは 3-1=2 のみなし出場"
    assert rested.adjusted == before + 2, "欠損は人数によらず1試合分"

    # 復帰後は枠の中で追いつく。ローテーションの揺れは slack+1 に収まる。
    sim.run(8)
    adjusted = [p.adjusted for p in sim.player_stats()]
    assert max(adjusted) - min(adjusted) <= 1


def test_returning_member_is_put_back_in_quickly():
    """休み明けはなるべく早くマッチに入れる（優先度7）。"""
    sim = Simulator(make_members(12), seed=606)
    sim.run(3)
    returning = sim.adopted_plans[-1].sitting_out[0]
    sim.set_status(returning, MemberStatus.RESTING)
    sim.run(2)
    sim.set_status(returning, MemberStatus.ACTIVE)

    assert sim.stat(returning).just_returned is True
    plan = sim.generate()
    assert returning in plan.playing


def test_late_joiner_does_not_monopolise_the_court():
    """途中参加者は下駄(baseline)のおかげで連続出場し続けたりしない。

    遅刻者の生の出場回数が少ないのは当然なので、そこは比べない。
    見るべきは、参加してから先の公平性（adjusted と、参加後の出場回数の増分）。
    """
    sim = Simulator(make_members(12), seed=8080)
    sim.run(8)
    established = min(p.adjusted for p in sim.player_stats())
    plays_before = {p.id: p.plays for p in sim.player_stats()}

    late = MemberSpec(id=99, nickname="遅刻", gender=Gender.FEMALE, level=Level.PICKLEBALL)
    sim.add_member(late)
    assert sim.stat(99).baseline == established, "下駄は参加時点の最小 adjusted"

    sim.run(8)
    stats = {p.id: p for p in sim.player_stats()}

    adjusted = [p.adjusted for p in stats.values()]
    assert max(adjusted) - min(adjusted) <= SLACK + 1, "参加後は adjusted で公平"

    # 参加してから先の出場回数の増分は、
    #   増分 = (最終の adjusted) - (参加時点の adjusted)
    # なので、そのばらつきは「最終の adjusted の差」と「参加時点の adjusted の差」の
    # 和までは広がりうる。どちらも公平性の枠 (SLACK + 1) に収まっている。
    gained = {i: p.plays - plays_before.get(i, 0) for i, p in stats.items()}
    assert max(gained.values()) - min(gained.values()) <= 2 * (SLACK + 1), (
        "参加してから先の出場回数も枠内に収まる"
    )
    assert stats[99].plays < max(p.plays for i, p in stats.items() if i != 99), (
        "遅刻した分の出場回数を取り返させはしない"
    )


def test_the_longest_waiting_member_is_picked_first():
    """出場回数が同じなら、長く待っている人から先に出す（優先度3）。

    9名2面では毎ラウンド1人だけ余る。出場回数も休み明けも揃えておくと、
    誰を余らせるかを決めるのは連続不参加の項だけになる。
    """
    waiting = player(1, sit_out_streak=2)
    others = [player(i) for i in range(2, 10)]

    for seed in range(5):
        plan = generate_round(
            [waiting, *others],
            History(),
            court_count=2,
            seed=seed,
            rng=make_rng(seed, 1, 0),
        )
        assert len(plan.sitting_out) == 1
        assert waiting.id in plan.playing, "長く待っている人をまた外している"


def test_a_member_back_from_a_rest_is_picked_first():
    """他の条件が同じなら、休み明けの人から先に出す（優先度7）。"""
    returning = player(1, just_returned=True)
    others = [player(i) for i in range(2, 10)]

    for seed in range(5):
        plan = generate_round(
            [returning, *others],
            History(),
            court_count=2,
            seed=seed,
            rng=make_rng(seed, 1, 0),
        )
        assert returning.id in plan.playing, "休み明けの人を待たせている"


@pytest.mark.parametrize("count", [9, 10, 12, 13, 16])
def test_consecutive_sit_outs_are_kept_short(count):
    """連続してマッチに入れない回数を最小にする（優先度3）。"""
    sim = Simulator(make_members(count), seed=2200 + count)
    sim.run(24)
    assert sim.max_sit_out_streak_seen() <= 2


# ---------------------------------------------------------------------------
# ばらけ（優先度1）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("count", "rounds"), [(13, 26), (16, 24), (9, 24)])
def test_partner_repeats_are_evened_out(count, rounds):
    """同じ相手とばかり組まない。重複回数は理想平均 + 1 に収まる。"""
    sim = Simulator(make_members(count), seed=515 + count)
    plans = sim.run(rounds)
    slots = plans[0].used_court_count * 4
    limit = max_partner_repeats_allowed(count, rounds, slots)
    counts = sim.partner_counts()
    assert max(counts.values()) <= limit


def test_alternating_halves_do_not_fix_the_pairings():
    """16名8枠では出場者が交代制になるが、組む相手は固定されない。

    16名で8枠なら、参加回数を完全に揃えるには1ラウンドおきに出場するしかない。
    出場者の交代自体は公平性から来る必然で、避けるべきものではない。
    避けたいのは「同じ8人でいつも同じペアを組む」ことなので、そちらを検証する。
    """
    sim = Simulator(make_members(16), seed=13579)
    sim.run(20)

    counts = sim.play_counts()
    assert max(counts.values()) - min(counts.values()) <= SLACK + 1

    partner_counts = sim.partner_counts()
    assert max(partner_counts.values()) <= max_partner_repeats_allowed(16, 20, 8)
    # 8人固定で組み続けると、作られるペアは高々 C(8,2) x 2 = 56 通りにしかならない。
    assert len(partner_counts) > 56, "出場グループが固定されてペアが偏っている"


def test_almost_everyone_shares_a_court_with_everyone():
    """3時間ぶん回せば、全員がほぼ全員と同じコートに入る。

    「いろんな人と当たれた」という体感に直結する性質。
    16名24ラウンドだと1人あたりの同席は 12試合 x 3人 = 36回で、相手は15人。
    全員と当たれるかどうかは回り方次第なので、全員必達は保証できない。

    閾値の根拠（2026-09-23 実測、シード 9000-9009 の10本）:
    「ペアの強さを揃える」減点（`strength_gap`）を入れると、左右の強さを合わせる分だけ
    対戦相手の自由度が減り、最少が 14人 から 13人 に下がるシードが出る。
    既定の 15 では 1本（seed=9000）、5 や 10 に下げると 2本で、下げても悪化は消えない。
    重みの調整では解けない＝機能そのものの代償。
    ユーザー判断で「強さを揃える」を優先し、ここは実測に合わせてある。

    最少だけだと1人の運で揺れるので、平均も併せて見張る。実測は 14.75〜15.00 で、
    15人中ほぼ全員と当たれている（取りこぼすのは1〜2人）。
    """
    rounds = 24
    for seed in (9000, 9001, 9002):
        sim = Simulator(make_members(16), seed=seed)
        plans = sim.run(rounds)

        met: dict[int, set[int]] = {member_id: set() for member_id in sim.specs}
        for plan in plans:
            for match in plan.matches:
                for member_id in match.member_ids:
                    met[member_id].update(set(match.member_ids) - {member_id})

        sizes = [len(partners) for partners in met.values()]
        others = len(sim.specs) - 1
        assert min(sizes) >= others - 2, (
            f"seed={seed}: 一度も当たっていない相手が多すぎる（最少 {min(sizes)} 人）"
        )
        average = sum(sizes) / len(sizes)
        assert average >= 14.5, (
            f"seed={seed}: 取りこぼしが増えている（平均 {average:.2f} 人 / {others} 人中）"
        )


def test_variety_stays_close_to_the_theoretical_optimum():
    """ペアと対戦の重複が、理論最小からどれだけ離れているかを見張る。

    同じ総数を可能なペアに均等配分したときの二乗和が理論最小値。重みに依存しないので、
    重みや探索を変えたときの品質の後退をこれで検出できる。
    閾値は実測（8シード）での貪欲法 23% / 先読みあり 18% の間に置いてある。
    """
    rounds = 24
    scores = []
    for seed in (7001, 7002, 7003):
        sim = Simulator(make_members(16, males=12), seed=seed)
        sim.run(rounds)
        scores.append(
            _variety_excess(sim.partner_counts(), rounds * 4, 16)
            + _variety_excess(sim.history.opponent_count, rounds * 8, 16)
        )
    assert sum(scores) / len(scores) <= 0.21


def test_opponents_are_varied_too():
    """対戦相手もばらける。"""
    sim = Simulator(make_members(13), seed=2468)
    sim.run(26)
    counts = sim.history.opponent_count
    assert max(counts.values()) <= max_partner_repeats_allowed(13, 26, 8) + 2


# ---------------------------------------------------------------------------
# 並び順の非依存性と非決定性（設計の要 D）
# ---------------------------------------------------------------------------


def test_result_does_not_depend_on_input_order():
    """入力リストの並び順を変えても結果は完全に一致する。"""
    players = make_members(13)
    sim = Simulator(players, seed=1234)
    sim.run(5)

    stats = sim.player_stats()
    baseline = sim.generate(players=stats, rng=make_rng(sim.seed, 6, 0))

    for shuffle_seed in range(5):
        shuffled = list(stats)
        random.Random(shuffle_seed).shuffle(shuffled)
        other = sim.generate(players=shuffled, rng=make_rng(sim.seed, 6, 0))
        assert other == baseline


def test_registration_order_does_not_leak_into_the_selection():
    """id の若い順に選ばれる、といった偏りが出ない。"""
    players = uniform_players(16)
    picked_first_half = 0
    mean_ids = []
    for seed in range(60):
        plan = generate_round(
            players, History(), court_count=2, seed=seed, rng=make_rng(seed, 1, 0)
        )
        if set(plan.playing) == set(range(1, 9)):
            picked_first_half += 1
        mean_ids.append(sum(plan.playing) / len(plan.playing))

    assert picked_first_half <= 1, "登録順の前半がそのまま選ばれている"
    average = sum(mean_ids) / len(mean_ids)
    assert 7.0 <= average <= 10.0, f"出場者の id が偏っている (平均 {average})"


def test_court_partners_are_not_biased_towards_small_ids():
    """同点だらけの状況で、誰と同じコートになるかが id に偏らない。

    3面以上では候補を刈り込むので、そこで同点の扱いを誤ると
    「id の小さい組だけが生き残る」形で登録順が透ける（不変則9/10）。
    ラウンド全体の署名はばらけて見えるため、個人ごとの同席分布で見る。
    """
    players = uniform_players(12)
    partners: Counter = Counter()
    for seed in range(120):
        plan = generate_round(
            players, History(), court_count=3, seed=seed, rng=make_rng(seed, 1, 0)
        )
        for match in plan.matches:
            if players[0].id in match.member_ids:
                partners.update(x for x in match.member_ids if x != players[0].id)

    assert len(partners) == 11, "同席していない相手がいる"
    average = sum(pid * n for pid, n in partners.items()) / sum(partners.values())
    assert 6.0 <= average <= 8.0, f"同席相手の id が偏っている（平均 {average:.2f}、一様なら 7.0）"


def test_tied_arrangements_are_drawn_uniformly():
    """同点の編成は先頭採用ではなく一様抽選する。

    属性が全員同じで履歴も無い8名では、315通りの編成がすべて同点になる。
    先頭を採る実装なら、どのシードでも同じ編成しか出ない。
    """
    players = uniform_players(8)
    trials = 300
    signatures = [
        generate_round(
            players, History(), court_count=2, seed=seed, rng=make_rng(seed, 1, 0)
        ).signature()
        for seed in range(trials)
    ]
    counts = Counter(signatures)
    assert len(counts) >= 150, f"出てくる編成が偏っている (相異なる編成 {len(counts)} 通り)"
    assert max(counts.values()) <= trials * 0.05


def test_different_sessions_play_out_differently():
    """同じ人数・同じ顔ぶれでも、練習会が変われば展開が変わる。"""
    players = make_members(13)
    signatures = set()
    for seed in range(20):
        sim = Simulator(players, seed=seed)
        plan = sim.generate()
        signatures.add(plan.signature())
    assert len(signatures) >= 15, (
        f"20シード中 {len(signatures)} 通りしか出ていない。"
        "練習会ごとに展開が変わると言えない"
    )


def test_same_seed_reproduces_the_same_result():
    """同じ練習会・同じ位置・同じ試行回数なら完全に再現する。"""
    players = make_members(13)
    first = Simulator(players, seed=777).generate()
    second = Simulator(players, seed=777).generate()
    assert first == second


def test_skipping_produces_a_different_card():
    """スキップすると別の編成が出る。"""
    sim = Simulator(make_members(13), seed=321)
    first = sim.generate()
    sim.reject(first)
    second = sim.generate()
    assert second.signature() != first.signature()


def test_rejected_arrangements_are_avoided_even_with_the_same_dice():
    """不採用にした編成そのものを避ける。

    スキップのたびに乱数は変わるので、たまたま別の編成が出ただけかもしれない。
    乱数を完全に同じにして avoid だけを変え、避ける仕組み自体が働くことを見る。
    """
    players = make_members(13)
    stats = Simulator(players, seed=321).player_stats()

    def draw(avoid):
        return generate_round(
            stats,
            History(),
            court_count=2,
            seed=321,
            rng=make_rng(321, 1, 0),
            avoid=avoid,
        )

    first = draw(())
    assert draw(()).signature() == first.signature(), "同じ乱数なら同じ結果になるはず"
    assert draw((first.signature(),)).signature() != first.signature()


def test_tie_key_is_stable_and_not_ordered_by_id():
    """tie_key は決定的で、id の順序とは無相関。"""
    assert tie_key(42, 7) == tie_key(42, 7)
    assert tie_key(42, 7) != tie_key(43, 7)
    keys = [tie_key(12345, i) for i in range(1, 30)]
    assert keys != sorted(keys), "id の昇順がそのまま順序になっている"


def test_four_players_have_exactly_three_pairings():
    """4人を1試合にする分け方は3通りしかない。"""
    assert len(PAIRINGS_OF_FOUR) == 3
    covered = {frozenset(map(frozenset, pairing)) for pairing in PAIRINGS_OF_FOUR}
    assert len(covered) == 3


def test_two_courts_enumerate_every_way_to_split_the_players():
    """2面（8人）の組み分けは35通りで、探索はその全部を見ている。

    1試合ずつ決める方式なので、コート数が増えても計算量は破綻しない。
    そのかわり多いときは刈り込むが、よく使う2面では全通りが残る。
    """
    stats = [player(i) for i in range(1, 9)]
    state = _make_state(stats)
    scorer = _make_scorer(stats, state)
    batches = split_into_matches(tuple(range(1, 9)), 2, scorer)
    assert len(batches) == 35
    assert len({frozenset(map(frozenset, batch)) for _cost, batch in batches}) == 35


def _reference_split_into_matches(ids, n_matches, scorer, beam_width):
    """`split_into_matches` の素直な実装。速くした実装と結果が同じかを照合する。

    全候補に組み分けのタプルと tie_break を付け、小さい順に beam_width 件を取る。
    """
    import heapq
    from itertools import combinations

    total = len(ids)
    states = [(0, (), 0)]
    for _ in range(n_matches):
        nxt = []
        for cost, groups, used in states:
            first = next(i for i in range(total) if not used >> i & 1)
            rest = [i for i in range(first + 1, total) if not used >> i & 1]
            for combo in combinations(rest, 3):
                indexes = (first, *combo)
                group = tuple(ids[i] for i in indexes)
                group_cost, _pairing = scorer.group_cost(group)
                mask = used
                for i in indexes:
                    mask |= 1 << i
                nxt.append((cost + group_cost, (*groups, group), mask))
        if len(nxt) <= beam_width:
            states = nxt
        else:
            keyed = [(c, scorer.tie_break(g), g, m) for c, g, m in nxt]
            states = [(c, g, m) for c, _key, g, m in heapq.nsmallest(beam_width, keyed)]
    return [(cost, groups) for cost, groups, _used in states]


def _scorer_for(stats, history, salt):
    from app.scheduler.generator import _Scorer, _State

    state = _State.from_history(stats, history)
    return _Scorer(stats, state, Weights(), rng=random.Random(salt), tie_salt=salt)


def _split_cases():
    """(ラベル, stats, history, コート数)。同点だらけの場合と、履歴が溜まった場合。"""
    cases = []
    for count in (12, 16):
        cases.append((f"全員同じ{count}人", uniform_players(count), History(), count // 4))
    for count, courts, beginners, racket in ((12, 3, 2, 2), (13, 3, 3, 2), (16, 4, 0, 0)):
        sim = Simulator(
            make_members(count, beginners=beginners, racket=racket), seed=31, court_count=courts
        )
        sim.run(6)
        cases.append((f"{count}人{courts}面・6ラウンド後", sim.player_stats(), sim.history, courts))
    return cases


@pytest.mark.parametrize("beam_width", [512, 16, 4])
def test_the_fast_split_matches_the_straightforward_one(beam_width):
    """速くした組み分け探索が、素直な実装と完全に同じ結果を返す。

    捨てる候補にはタプルもハッシュも作らないようにしたが、残す候補の集合も
    並び順も変えてはならない（並び順は後段の乱数の引き方に効く）。
    刈り込み幅を小さくして、境目での同点を多く起こす。
    """
    for label, stats, history, courts in _split_cases():
        active = [p for p in stats if p.status is MemberStatus.ACTIVE]
        ids = tuple(p.id for p in active)[: courts * 4]
        reference_scorer = _scorer_for(active, history, salt=7)
        fast_scorer = _scorer_for(active, history, salt=7)

        expected = _reference_split_into_matches(ids, courts, reference_scorer, beam_width)
        actual = split_into_matches(ids, courts, fast_scorer, beam_width=beam_width)

        assert actual == expected, label
        # 4人のペア分けの抽選（乱数を引く順）も変わっていない。
        assert fast_scorer._group_cache == reference_scorer._group_cache, label


def test_tie_break_bytes_are_unchanged():
    """tie_break の入力は、塩と member_id を1つずつ pack して繋いだものと同じ。"""
    import hashlib
    import struct

    scorer = _scorer_for(uniform_players(8), History(), salt=123)
    groups = ((1, 2, 3, 4), (5, 6, 7, 8))
    material = struct.pack("<q", 123) + b"".join(
        struct.pack("<q", member_id) for group in groups for member_id in group
    )
    expected = int.from_bytes(hashlib.blake2b(material, digest_size=8).digest(), "little")
    assert scorer.tie_break(groups) == expected


# ---------------------------------------------------------------------------
# 初心者（優先度3〜5, 6d）
# ---------------------------------------------------------------------------


def beginner_pairs_in(plan: RoundPlan, beginner_ids: set[int]) -> int:
    return sum(
        1
        for match in plan.matches
        for team in (match.team_a, match.team_b)
        if set(team) <= beginner_ids
    )


@pytest.mark.parametrize(("count", "beginners"), [(16, 2), (13, 2), (13, 3), (10, 2)])
def test_beginners_are_never_paired_together(count, beginners):
    """初心者同士のペアは作らない（優先度3）。"""
    sim = Simulator(make_members(count, beginners=beginners), seed=6400 + count)
    plans = sim.run(24)
    beginner_ids = set(range(1, beginners + 1))
    assert sum(beginner_pairs_in(plan, beginner_ids) for plan in plans) == 0


@pytest.mark.parametrize(
    ("count", "courts", "beginners"),
    [(12, 3, 3), (13, 3, 3), (16, 4, 3), (16, 4, 4)],
)
def test_beginners_are_not_paired_even_when_registered_last(count, courts, beginners):
    """初心者が後から登録されても（= id が大きくても）同士ペアを作らない。

    組み分けの探索は「まだ使っていない中で先頭の人」を起点に進むので、
    制約の強い人が末尾に固まると、最後の組で避けようがなくなる。
    3面以上でないと刈り込みが起きず、初心者を先頭に置いた構成では踏めない。
    初心者は公募で後から入ることが多く、実運用ではこちらが普通。
    """
    members = make_members(count, beginners=beginners, beginners_last=True)
    beginner_ids = {m.id for m in members if m.level is Level.BEGINNER}
    sim = Simulator(members, seed=7700 + count, court_count=courts)
    plans = sim.run(16)
    offenders = sum(beginner_pairs_in(plan, beginner_ids) for plan in plans)
    assert offenders == 0, f"初心者同士ペアが {offenders} 件できている"


def test_beginner_pairs_face_each_other():
    """初心者2名が同時に出るときは、同じコートで対戦させる（優先度5）。

    初心者が入る試合は面白さが落ちるので、その影響を1コートにまとめる。
    """
    sim = Simulator(make_members(16, beginners=2), seed=8642)
    plans = sim.run(24)
    beginner_ids = {1, 2}
    both_playing = 0
    same_court = 0
    for plan in plans:
        if len(beginner_ids & set(plan.playing)) < 2:
            continue
        both_playing += 1
        if any(len(beginner_ids & set(m.member_ids)) == 2 for m in plan.matches):
            same_court += 1
    assert both_playing >= 5, "テストの前提: 両名が同時に出るラウンドが十分あること"
    assert same_court >= both_playing * 0.9


def test_partnering_a_beginner_is_shared_evenly():
    """初心者と組む回数を非初心者の間で均す（優先度4）。

    閾値は 1。許容を 2 にすると、均す機構（`beginner_spread`）を殺したときの
    実測値とちょうど同じになり、仕様違反を検出できなくなる（下のテスト参照）。
    """
    sim = Simulator(make_members(16, beginners=2), seed=1357)
    sim.run(24)
    counts = [sim.history.beginner_partners(i) for i in range(3, 17)]
    assert max(counts) - min(counts) <= 1


def test_the_beginner_burden_is_uneven_without_the_mechanism():
    """上のテストの閾値が意味を持つことを、対照で確かめる。

    `beginner_spread` を切ると受け持ち回数がばらつくことを示す。これが無いと
    「閾値が緩すぎて何も検出していない」状態に気づけない。
    """
    weights = dataclasses.replace(Weights(), beginner_spread=0)
    sim = Simulator(make_members(16, beginners=2), seed=1357, weights=weights)
    sim.run(24)
    counts = [sim.history.beginner_partners(i) for i in range(3, 17)]
    assert max(counts) - min(counts) > 1, (
        "機構を切ってもばらつかないなら、上の閾値は何も見張っていない"
    )


def test_rule_unaware_players_are_not_paired_together():
    """ルールを覚えていない者どうしでペアを組ませない（仕様 3a/3b/3c）。

    初心者はボールが返せず試合が成立しない。ラケット経験者は1試合で慣れるので、
    組んでしまっても傷は浅い。優先度はその順。
    """
    members = make_members(16, males=8, beginners=2, racket=3)
    sim = Simulator(members, seed=6400)
    sim.run(24)
    counts = sim.unaware_pair_counts()
    assert counts["beginner-beginner"] == 0
    assert counts["beginner-racket"] == 0
    # 避けられる構成なので、ラケット経験者同士もほとんど出ない
    assert counts["racket-racket"] <= 2


def test_rule_unaware_pairs_degrade_in_the_order_of_the_spec():
    """避けきれない人数構成では、優先度の低いものから先に崩れる。

    16名全員がラケット経験者なら、どう組んでもラケット経験者同士になる。
    そういう場合でも破綻せず、初心者を含む組み合わせから先に守られる。
    """
    members = make_members(16, males=8, beginners=2, racket=14)
    sim = Simulator(members, seed=99)
    sim.run(12)
    counts = sim.unaware_pair_counts()
    assert counts["beginner-beginner"] == 0, "初心者同士だけは最後まで守る"
    assert counts["racket-racket"] > 0, "テストの前提: 避けられない構成であること"


def test_matches_are_between_pairs_of_similar_strength():
    """対等なペア同士のマッチを増やす（仕様 5a）。

    強さはレベルと性別で見積もる。20点満点の尺度で、平均の差が小さいほどよい。
    """
    sim = Simulator(make_members(16, males=8, beginners=2, racket=3), seed=42)
    sim.run(24)
    gaps = sim.strength_gaps()
    assert sum(gaps) / len(gaps) <= 1.5
    assert max(gaps) <= 8


def test_strength_ignores_gender_for_beginners():
    """初心者は男女を区別しない。

    ボールが返せるかどうかの段階なので、パワーの差が意味を持たない。
    """
    male = player(1, gender=Gender.MALE, level=Level.BEGINNER)
    female = player(2, gender=Gender.FEMALE, level=Level.BEGINNER)
    assert male.strength == female.strength == 0

    # 初心者以外は性別で差が付く
    assert player(3, gender=Gender.MALE, level=Level.PICKLEBALL).strength > player(
        4, gender=Gender.FEMALE, level=Level.PICKLEBALL
    ).strength


def test_the_level_gap_is_larger_below_than_above():
    """初心者とラケット経験者の差は、ラケット経験者とピックルボール経験者の差より大きい。

    仕様では3倍程度としている。
    """
    beginner = Level.BEGINNER.strength
    racket = Level.RACKET_EXPERIENCED.strength
    pickleball = Level.PICKLEBALL.strength
    assert beginner < racket < pickleball
    assert (racket - beginner) == 3 * (pickleball - racket)


def test_beginner_pairs_ignore_gender():
    """初心者を含むペアは男女構成の評価から外す（仕様 6d）。"""
    beginner = player(1, gender=Gender.MALE, level=Level.BEGINNER)
    male = player(2, gender=Gender.MALE)
    female = player(3, gender=Gender.FEMALE)
    assert _pair_kind(beginner, male) is PairKind.MX
    assert _pair_kind(beginner, female) is PairKind.MX
    assert _pair_kind(male, player(4, gender=Gender.MALE)) is PairKind.MM


def test_other_gender_is_treated_as_mixed():
    """その他/未回答を含むペアは減点しない。"""
    other = player(1, gender=Gender.OTHER)
    assert _pair_kind(other, player(2, gender=Gender.MALE)) is PairKind.MX
    assert _pair_kind(other, player(3, gender=Gender.FEMALE)) is PairKind.MX
    assert _pair_kind(other, player(4, gender=Gender.OTHER)) is PairKind.MX


# ---------------------------------------------------------------------------
# 男女構成（優先度6）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "males", "rounds"),
    [(16, 8, 24), (16, 12, 24), (13, 8, 26), (9, 5, 24), (10, 6, 24)],
)
def test_male_pair_never_faces_female_pair(count, males, rounds):
    """男子ペア対女子ペアは作らない（仕様 6a）。

    少数派の性別を各ペアに散らせば必ず回避できるので、この構成が必要になる場面は無い。
    """
    sim = Simulator(make_members(count, males=males), seed=1700 + count)
    sim.run(rounds)
    assert sim.gender_pattern_counts().get("ff-mm", 0) == 0


def test_balanced_group_mostly_plays_mixed_versus_mixed():
    """男女が揃っているなら、男女ペア同士の試合を主にする（仕様 6）。"""
    sim = Simulator(make_members(16, males=8), seed=2024)
    sim.run(24)
    patterns = sim.gender_pattern_counts()
    total = sum(patterns.values())
    assert patterns.get("mx-mx", 0) / total >= 0.5


def test_unbalanced_group_uses_same_gender_matchups_as_the_adjustment():
    """男女数が偏ったときの調整は、男子ペア対男子ペアで行う（仕様 6b）。"""
    sim = Simulator(make_members(16, males=12), seed=4096)
    sim.run(24)
    patterns = sim.gender_pattern_counts()
    assert patterns.get("mm-mm", 0) > 0
    assert patterns.get("ff-mm", 0) == 0


# ---------------------------------------------------------------------------
# コートが埋まらないケース（8名未満）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [4, 5, 6, 7])
def test_only_one_court_is_used_when_there_are_not_enough_players(count):
    """8名未満では1面だけ使い、もう1面は未使用とわかるようにする。"""
    sim = Simulator(make_members(count), seed=300 + count)
    plan = sim.generate()
    assert plan.used_court_count == 1
    assert plan.unused_court_indexes == (1,)
    assert plan.matches[0].court_index == 0
    assert len(plan.playing) == 4
    assert len(plan.sitting_out) == count - 4
    assert_plan_is_consistent(plan, court_count=2)


@pytest.mark.parametrize("count", [5, 6, 7])
def test_members_left_out_of_a_single_court_round_play_next(count):
    """1面しか使えないときに余った人は、次のラウンドで出場する。"""
    sim = Simulator(make_members(count), seed=500 + count)
    first = sim.generate()
    sim.adopt(first)
    second = sim.generate()
    assert set(first.sitting_out) <= set(second.playing)


def test_seven_members_share_a_single_court_fairly():
    """7名1面でも参加回数は揃い、連続不参加も短く保たれる。"""
    sim = Simulator(make_members(7), seed=7007)
    sim.run(14)
    counts = sim.play_counts()
    assert max(counts.values()) - min(counts.values()) <= 1
    assert sim.max_sit_out_streak_seen() <= 2


def test_second_court_opens_when_an_eighth_member_arrives():
    """7名→8名になったら、次の生成から2面とも使う。"""
    sim = Simulator(make_members(7), seed=1212)
    sim.run(3)
    assert sim.generate().used_court_count == 1

    sim.add_member(MemberSpec(id=8, nickname="8人目", gender=Gender.FEMALE))
    plan = sim.generate()
    assert plan.used_court_count == 2
    assert plan.unused_court_indexes == ()


def test_second_court_closes_when_a_member_leaves():
    """8名→7名になったら、次の生成から1面だけになる。"""
    sim = Simulator(make_members(8), seed=2121)
    sim.run(3)
    assert sim.generate().used_court_count == 2

    sim.set_status(8, MemberStatus.LEFT)
    plan = sim.generate()
    assert plan.used_court_count == 1
    assert plan.unused_court_indexes == (1,)


def test_resting_members_do_not_occupy_a_court():
    """休憩中の人は出場者に数えない。"""
    sim = Simulator(make_members(9), seed=3131)
    sim.run(2)
    for member_id in (1, 2):
        sim.set_status(member_id, MemberStatus.RESTING)
    plan = sim.generate()
    assert plan.used_court_count == 1
    assert set(plan.resting) == {1, 2}
    assert not set(plan.playing) & {1, 2}


# ---------------------------------------------------------------------------
# 13名のケース（もっともありがちな規模）
# ---------------------------------------------------------------------------


def test_thirteen_members_realistic_session():
    """13名（男8/女5、初心者1名）で3時間ぶん回したときの総合的な振る舞い。"""
    members = make_members(13, males=8, beginners=1)
    sim = Simulator(members, seed=20260922)
    plans = sim.run(26)

    counts = sim.play_counts()
    assert max(counts.values()) - min(counts.values()) <= SLACK + 1
    assert sim.max_sit_out_streak_seen() <= 2
    assert sum(beginner_pairs_in(plan, {1}) for plan in plans) == 0
    assert sim.gender_pattern_counts().get("ff-mm", 0) == 0
    assert max(sim.partner_counts().values()) <= max_partner_repeats_allowed(13, 26, 8)
    for plan in plans:
        assert_plan_is_consistent(plan, court_count=2)


# ---------------------------------------------------------------------------
# 境界・縮退
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [4, 5, 6, 7, 8, 9, 13, 16])
def test_plans_are_always_consistent(count):
    sim = Simulator(make_members(count), seed=900 + count)
    for plan in sim.run(3):
        assert_plan_is_consistent(plan, court_count=2)


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_too_few_players_is_rejected(count):
    players = make_members(count)
    sim = Simulator(players, seed=1)
    with pytest.raises(NotEnoughPlayersError):
        sim.generate()


def test_all_members_resting_is_rejected():
    sim = Simulator(make_members(8), seed=1)
    for member_id in range(1, 9):
        sim.set_status(member_id, MemberStatus.RESTING)
    with pytest.raises(NotEnoughPlayersError):
        sim.generate()


def test_single_gender_group_does_not_break():
    sim = Simulator(make_members(12, males=12), seed=99)
    for plan in sim.run(6):
        assert_plan_is_consistent(plan, court_count=2)


def test_all_beginners_still_produces_a_card():
    """全員初心者なら初心者同士ペアは避けられないが、破綻はしない。"""
    sim = Simulator(make_members(8, beginners=8), seed=55)
    for plan in sim.run(3):
        assert_plan_is_consistent(plan, court_count=2)


def test_single_court_session_is_supported():
    """コート1面の練習会でも動く（データモデル上はコート数可変）。"""
    sim = Simulator(make_members(10), seed=11, court_count=1)
    plan = sim.generate()
    assert plan.used_court_count == 1
    assert plan.unused_court_indexes == ()
    assert_plan_is_consistent(plan, court_count=1)


def test_widening_the_envelope_is_bounded_by_the_configured_slack():
    """枠を広げる設定にしても、広がるのは設定した試合数ぶんだけ。

    既定は厳密公平だが、もっとシャッフルしたい場合のために枠を広げられる。
    広げすぎて不公平が青天井にならないことを確認する。
    """
    sim = Simulator(make_members(16), seed=2468, fairness_slack=1)
    sim.run(24)
    counts = sim.play_counts()
    assert max(counts.values()) - min(counts.values()) <= 1 + 1


# ---------------------------------------------------------------------------
# 仕様の優先順位そのものを固定する
#
# 「結果として b-b が 0 件」といった観測だけだと、3b と 3c を入れ替えても
# 誰も落ちない。順序は重み・コストの大小として直接おさえる。
# ---------------------------------------------------------------------------


def _pair_cost(level_a: Level, level_b: Level) -> int:
    """2人だけのペアコスト。履歴は空なので、レベルの効果だけが出る。"""
    a = player(1, level=level_a)
    b = player(2, level=level_b)
    stats = [a, b]
    scorer = _make_scorer(stats, _make_state(stats))
    return scorer._compute_pair_cost(a, b, tie_key(0, a.id) < tie_key(0, b.id) and (1, 2) or (2, 1))


def test_rule_unaware_pair_costs_follow_the_spec_order():
    """3a > 3b > 3c > 習得済み同士、の順に避ける（仕様 3a/3b/3c）。"""
    beginner_pair = _pair_cost(Level.BEGINNER, Level.BEGINNER)
    mixed_pair = _pair_cost(Level.BEGINNER, Level.RACKET_EXPERIENCED)
    racket_pair = _pair_cost(Level.RACKET_EXPERIENCED, Level.RACKET_EXPERIENCED)
    known_pair = _pair_cost(Level.PICKLEBALL, Level.PICKLEBALL)

    assert beginner_pair > mixed_pair, "初心者同士がいちばん避けたい（3a）"
    assert mixed_pair > racket_pair, "初心者 x ラケット経験者の方が重い（3b > 3c）"
    assert racket_pair > known_pair, "ルールを覚えていない者同士は避ける（3c）"


def test_gender_matchup_costs_follow_the_spec_order():
    """6 / 6a / 6b / 6c の優先順位を、コストの大小で固定する。

    「男女ペア同士がよい」「男子対女子は避ける」だけでなく、その間にある
    6b（mm 対 mm / ff 対 ff がベストな調整）と 6c（mx 対 mm / mx 対 ff が次点）
    の順序も含めて押さえる。
    """
    w = Weights()
    mm, ff, mx = PairKind.MM, PairKind.FF, PairKind.MX

    assert gender_cost(mx, mx, w) == 0, "男女ペア同士が最良（6）"
    assert gender_cost(mx, mx, w) < gender_cost(mm, mm, w)
    assert gender_cost(mm, mm, w) == gender_cost(ff, ff, w), "6b は男女で対称"
    assert gender_cost(mm, mm, w) < gender_cost(mm, mx, w), "6b が 6c より良い"
    assert gender_cost(mm, mx, w) == gender_cost(ff, mx, w), "6c は男女で対称"
    assert gender_cost(mm, mx, w) < gender_cost(mm, ff, w), "6a が最も避けたい"


def test_a_returning_member_wins_a_tie_against_equals():
    """出場回数が並んだら、休み明けを先に入れる（優先度7）。

    公平性の枠が先に効くので、出場回数に差があるうちは重みを切っても
    休み明けが選ばれる。重み `just_returned` が実際に効くのは、
    条件が並んだときにどちらを採るかという場面だけ。そこを直接見る。
    """
    others = [player(i, plays=1) for i in range(1, 5)]
    returning = player(5, plays=1, just_returned=True)
    players = [*others, returning]

    def benched(weights: Weights) -> int:
        """休み明けが外された回数。"""
        count = 0
        for seed in range(20):
            plan = generate_round(
                players,
                History(),
                court_count=1,
                seed=seed,
                rng=make_rng(seed, 1, 0),
                weights=weights,
            )
            if returning.id not in plan.playing:
                count += 1
        return count

    assert benched(Weights()) == 0, "並んだら休み明けを必ず入れる"
    assert benched(dataclasses.replace(Weights(), just_returned=0)) > 0, (
        "重みを切れば外れることがある（＝この重みが優先度7を担っている）"
    )


def test_a_beginner_pair_can_face_a_female_pair():
    """初心者を含むペアは性別を無視する（仕様 6d）。

    6a「男子ペア対女子ペア」は実質ハード制約だが、初心者を含むペアには
    かからない。単体の `_pair_kind` だけでなく、実際に生成できることを見る。
    """
    members = make_members(8, males=4, beginners=2)
    sim = Simulator(members, seed=2468)
    plans = sim.run(12)

    beginners = {m.id for m in sim.specs.values() if m.level is Level.BEGINNER}
    saw_beginner_pair = False
    for plan in plans:
        for match in plan.matches:
            for team in (match.team_a, match.team_b):
                if set(team) & beginners:
                    saw_beginner_pair = True
    assert saw_beginner_pair, "テストの前提: 初心者が出場している"

    # 6a に阻まれて生成不能になっていないこと（初心者ペアが毎回できている）
    assert len(plans) == 12
