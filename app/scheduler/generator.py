"""対戦カードの生成。

設計の要点:

* **1試合ずつ作って、順にコートへ流し込む。** コート数に依存しない構造にしてある。
  1ラウンドを全コートまとめて全列挙すると、コート数の指数で組合せが爆発する
  （8人なら315通りだが、12人では155,925通り、16人ではさらにその数百倍）。
* スコアは1試合ごとのコストの和に **厳密に** 分解できる。
  「出場しなかった人への減点」は「出場した人への加点」に置き換えられる
  （両者の合計はそのラウンドでは定数なので）。近似ではなく等価な変形。
* 1ラウンドだけを見た貪欲な選択は、その先で組める相手を減らしてしまうことがある。
  そこで上位候補について数ラウンド先までロールアウトし、累積コストで選び直す。
* 公平性は候補集合を絞るハードな枠として効かせ、その枠内で「ばらけ」を最適化する。
* 入力の並び順は結果に影響しない。同点は先頭を採らず一様抽選する。

このモジュールは DB と Web を知らない（CLAUDE.md 不変則1）。
"""

from __future__ import annotations

import hashlib
import heapq
import math
import random
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

from app.errors import NotEnoughPlayersError
from app.scheduler.domain import (
    Gender,
    History,
    MatchPlan,
    MemberStatus,
    PairKind,
    PlayerStat,
    RoundPlan,
    Weights,
    pair_key,
)

PLAYERS_PER_MATCH = 4
PLAYERS_PER_TEAM = 2

#: 4人を1試合にするときのペアの分け方。3通りしかない。
PAIRINGS_OF_FOUR = (((0, 1), (2, 3)), ((0, 2), (1, 3)), ((0, 3), (1, 2)))

#: 1ラウンドの組み分けを探すとき、各段で残す候補数。
#: 8人（2面）なら分け方は35通りしかないので、この値で全通りが残り厳密になる。
#: 3面以上では刈り込みが入るが、コート数が増えても計算量は破綻しない。
#:
#: 仕様3a（初心者同士ペア）を守るのはこの値ではなく、出場者を制約の強い順に
#: 並べること（`_plan_round` を参照）。並べ替えを入れれば 64 でも 0 件になる。
#:
#: この値が効くのは優先度5（初心者ペアの集中）。履歴が溜まると差が出る。
#: 12人3面・初心者3名・24ラウンド・10シードの実測:
#:     64 → 片側だけ初心者 2.07/ラウンド（理想 1.0）、ばらけ超過 0.076、85ms
#:    512 → 1.73、0.080、96ms
#: ばらけ（優先度1）がわずかに悪化するが、集中（優先度5）の改善の方が大きい。
BATCH_BEAM = 512

#: 先読みの中での組み分け探索はもっと粗くてよい。
ROLLOUT_BATCH_BEAM = 16

#: ロールアウト中に評価する出場者集合の上限。先読みは本番の選択ほど精密でなくてよい。
ROLLOUT_CANDIDATE_SETS = 6

#: 試合の男女構成ごとの減点の係数（仕様 6/6b/6c）。値が小さいほど望ましい。
#:
#: 男女ペア同士がベスト(0)。男女数が非均一なときの調整としては
#: 男子ペア対男子ペア / 女子ペア対女子ペアが最良(1)、
#: 男女ペア対単一性別ペアが次点(2)。
#:
#: これらは仕様で優先度6、ばらけは優先度1なので、
#: ここでの減点はペア重複1回ぶんより小さくなるように重みを選ぶ。
_GENDER_PENALTY: dict[tuple[PairKind, PairKind], int] = {
    (PairKind.MX, PairKind.MX): 0,
    (PairKind.MM, PairKind.MM): 1,
    (PairKind.FF, PairKind.FF): 1,
    (PairKind.MM, PairKind.MX): 2,
    (PairKind.FF, PairKind.MX): 2,
}

#: 男子ペア対女子ペア（仕様 6a）。少数派の性別を各ペアに散らせば必ず回避できるので、
#: この構成が必要になる場面は無い。別枠の重み ``gender_split`` で実質的に禁止する。
_SPLIT_GENDERS = (PairKind.FF, PairKind.MM)

Pair = tuple[int, int]
"""ペア。member_id 2つを昇順に並べたもの。"""

Group = tuple[int, int, int, int]
"""1試合に出る4人。member_id を昇順に並べたもの。"""

Batch = tuple[Group, ...]
"""1ラウンド分の組み分け。コートに流し込む順に並んでいる。"""


# ---------------------------------------------------------------------------
# 決定的な乱数
# ---------------------------------------------------------------------------


def tie_key(seed: int, member_id: int) -> int:
    """並び順の正規化に使う、決定的だが member_id と無相関な値。

    登録順や id 順がマッチに透けて見えることを防ぐ（CLAUDE.md 不変則9）。
    プロセスをまたいで安定させたいので、ハッシュ関数には blake2b を使う
    （Python の ``hash()`` は int に対しては恒等関数なので使えない）。
    """
    digest = hashlib.blake2b(struct.pack("<qq", seed, member_id), digest_size=8).digest()
    return int.from_bytes(digest, "little")


def make_rng(seed: int, round_seq: int, attempt: int) -> random.Random:
    """生成1回分の乱数生成器を、練習会のシードから決定的に導出する。

    同じ練習会・同じ位置・同じ試行回数なら必ず同じ結果になる（不変則11）。
    ``attempt`` はスキップした回数なので、スキップのたびに別の抽選になる。
    """
    digest = hashlib.blake2b(
        struct.pack("<qqq", seed, round_seq, attempt), digest_size=16
    ).digest()
    return random.Random(int.from_bytes(digest, "little"))


def normalize_players(players: Sequence[PlayerStat], seed: int) -> list[PlayerStat]:
    """入力の並び順を打ち消す。"""
    return sorted(players, key=lambda p: tie_key(seed, p.id))


def _pair_kind(a: PlayerStat, b: PlayerStat) -> PairKind:
    """ペアの男女構成。

    減点しないケースが2つある（いずれも :attr:`PairKind.MX` を返す）。

    * ``OTHER`` を含むペア。男女の区分が付かないので評価から外す。
    * 初心者を含むペア（仕様 6d）。初心者が入る試合は育成目的なので、
      男女構成の良し悪しは問わない。こうしておくと「初心者を集める」目的と
      「男女ペア同士にする」目的が衝突しなくなる。
    """
    if a.is_beginner or b.is_beginner:
        return PairKind.MX
    if a.gender is Gender.MALE and b.gender is Gender.MALE:
        return PairKind.MM
    if a.gender is Gender.FEMALE and b.gender is Gender.FEMALE:
        return PairKind.FF
    return PairKind.MX


def gender_cost(kind_a: PairKind, kind_b: PairKind, weights: Weights) -> int:
    """試合の男女構成に対する減点。"""
    key = (kind_a, kind_b) if kind_a.value <= kind_b.value else (kind_b, kind_a)
    if key == _SPLIT_GENDERS:
        return weights.gender_split
    return weights.gender * _GENDER_PENALTY[key]


# ---------------------------------------------------------------------------
# ロールアウト用の可変状態
# ---------------------------------------------------------------------------


@dataclass
class _State:
    """スコア計算に使う、その時点の履歴と出場状況。

    先読みではこれを複製して書き換えながら数ラウンド進める。
    """

    partner: dict[Pair, int]
    opponent: dict[Pair, int]
    beginner_partner: dict[int, int]
    adjusted: dict[int, int]
    sit_out_streak: dict[int, int]
    just_returned: set[int]

    @classmethod
    def from_history(cls, active: Sequence[PlayerStat], history: History) -> _State:
        return cls(
            partner=dict(history.partner_count),
            opponent=dict(history.opponent_count),
            beginner_partner=dict(history.beginner_partner_count),
            adjusted={p.id: p.adjusted for p in active},
            sit_out_streak={p.id: p.sit_out_streak for p in active},
            just_returned={p.id for p in active if p.just_returned},
        )

    def copy(self) -> _State:
        return _State(
            partner=dict(self.partner),
            opponent=dict(self.opponent),
            beginner_partner=dict(self.beginner_partner),
            adjusted=dict(self.adjusted),
            sit_out_streak=dict(self.sit_out_streak),
            just_returned=set(self.just_returned),
        )

    def apply(
        self,
        pairings: Sequence[tuple[Pair, Pair]],
        active_ids: Sequence[int],
        beginners: frozenset[int],
    ) -> None:
        """1ラウンドぶん進める。"""
        playing: set[int] = set()
        for pair_a, pair_b in pairings:
            for team in (pair_a, pair_b):
                playing.update(team)
                self.partner[team] = self.partner.get(team, 0) + 1
                first, second = team
                if (first in beginners) != (second in beginners):
                    non_beginner = second if first in beginners else first
                    self.beginner_partner[non_beginner] = (
                        self.beginner_partner.get(non_beginner, 0) + 1
                    )
            for x in pair_a:
                for y in pair_b:
                    key = pair_key(x, y)
                    self.opponent[key] = self.opponent.get(key, 0) + 1

        for member_id in active_ids:
            if member_id in playing:
                self.adjusted[member_id] += 1
                self.sit_out_streak[member_id] = 0
                self.just_returned.discard(member_id)
            else:
                self.sit_out_streak[member_id] += 1


# ---------------------------------------------------------------------------
# スコア
# ---------------------------------------------------------------------------


def player_costs(
    active: Sequence[PlayerStat], state: _State, weights: Weights
) -> dict[int, int]:
    """その人を出場させることの得失。小さいほど出したい。

    仕様の優先度2（参加回数の公平）・3（連続不参加を短く）・7（休み明けを優先）は、
    本来「出さなかった人への減点」として書ける。ただしそのラウンドで出す人数は
    決まっているので、出場者と非出場者の減点の合計は定数になる。
    そこで符号を反転して「出場者への加点」にまとめ直してある。
    こうすると評価を1試合ごとに分解でき、コート数に依存しない探索ができる。

    ただし符号を反転した分の定数は :func:`benched_baseline` で足し戻すこと。
    1ラウンドの中では定数なので順位は変わらないが、先読みでは分岐ごとに
    状態（＝この定数）が変わるため、落とすと累積コストの比較が狂う。
    """
    w = weights
    costs: dict[int, int] = {}
    for player in active:
        cost = w.fair * (2 * state.adjusted[player.id] + 1)
        cost -= w.sit_out * (state.sit_out_streak[player.id] + 1) ** 2
        if player.id in state.just_returned:
            cost -= w.just_returned
        costs[player.id] = cost
    return costs


def benched_baseline(
    active: Sequence[PlayerStat], state: _State, weights: Weights
) -> int:
    """:func:`player_costs` で符号を反転した分の定数。

    「非出場者への減点」の合計は、出場者と非出場者の両方を足した値から
    出場者ぶんを引いたもの。前者がこの定数にあたる。
    """
    w = weights
    total = sum(w.sit_out * (state.sit_out_streak[p.id] + 1) ** 2 for p in active)
    total += sum(w.just_returned for p in active if p.id in state.just_returned)
    return total


class _Scorer:
    """ペア単位・試合単位のコストを計算する。

    これらのコストは「誰が出るか」には依存せず、履歴と2人（または4人）だけで決まる。
    そのため1ラウンドの探索につき1度だけ作り、すべての候補集合で使い回す。
    """

    def __init__(
        self,
        players: Sequence[PlayerStat],
        state: _State,
        weights: Weights,
        *,
        rng: random.Random,
        avoid_matches: frozenset = frozenset(),
        tie_salt: int = 0,
    ) -> None:
        self._state = state
        self._weights = weights
        self._rng = rng
        # 刈り込みの同点をほぐすための塩。練習会のシードから渡す。
        # ここで乱数を引くと乱数列がずれ、先読みが候補を同じ運の下で
        # 比べられなくなる（共通乱数法が崩れる）。
        self._tie_salt = tie_salt
        self._avoid_matches = avoid_matches
        self._match_cost_cache: dict[tuple[Pair, Pair], int] = {}
        self._group_cache: dict[Group, tuple[int, tuple[Pair, Pair]]] = {}

        self.pair_cost: dict[Pair, int] = {}
        self.pair_kind: dict[Pair, PairKind] = {}
        self.pair_has_beginner: dict[Pair, int] = {}
        self.pair_strength: dict[Pair, int] = {}

        for a, b in combinations(players, 2):
            key = pair_key(a.id, b.id)
            self.pair_kind[key] = _pair_kind(a, b)
            self.pair_has_beginner[key] = int(a.is_beginner or b.is_beginner)
            self.pair_strength[key] = a.strength + b.strength
            self.pair_cost[key] = self._compute_pair_cost(a, b, key)

    def tie_break(self, groups: Batch) -> int:
        """同点の候補を刈り込むときの並べ替えキー。

        乱数を引かずに決めるので、候補をまたいで乱数列がずれない。
        member_id と無相関なので、id の小さい組だけが生き残ることもない
        （不変則9/10）。塩はラウンドごとに変わる。
        """
        # 塩と member_id を1回の pack で並べる。1つずつ pack して繋いだものと同じバイト列。
        flat = [member_id for group in groups for member_id in group]
        material = struct.pack(f"<{len(flat) + 1}q", self._tie_salt, *flat)
        return int.from_bytes(hashlib.blake2b(material, digest_size=8).digest(), "little")

    def _compute_pair_cost(self, a: PlayerStat, b: PlayerStat, key: Pair) -> int:
        w = self._weights
        # 優先度1: 同じ相手とばかり組まないようにする。
        # 増分 2n+1 は「ペアを組んだ回数の二乗和」を最小化する = 回数を均す。
        cost = w.partner * (2 * self._state.partner.get(key, 0) + 1)

        # 優先度3: ルールを覚えていない者同士でペアを組ませない。
        # 初心者はボールが返せず試合が成立しないので最も強く避ける。
        # ラケット経験者は1試合で慣れるので、組ませてしまっても傷は浅い。
        if not a.knows_rules and not b.knows_rules:
            if a.is_beginner and b.is_beginner:
                cost += w.beginner_pair
            elif a.is_beginner or b.is_beginner:
                cost += w.beginner_racket_pair
            else:
                cost += w.racket_pair

        # 優先度4: 初心者と組む回数を、初心者以外の間で均等にする。
        if a.is_beginner != b.is_beginner:
            non_beginner = b.id if a.is_beginner else a.id
            cost += w.beginner_spread * (
                2 * self._state.beginner_partner.get(non_beginner, 0) + 1
            )
        return cost

    def match_cost(self, pair_a: Pair, pair_b: Pair) -> int:
        """1試合分のコスト（対戦の重複と男女構成）。"""
        key = (pair_a, pair_b) if pair_a <= pair_b else (pair_b, pair_a)
        cached = self._match_cost_cache.get(key)
        if cached is not None:
            return cached

        w = self._weights
        # 優先度1: 同じ相手とばかり対戦しないようにする。
        opponent = self._state.opponent
        cost = sum(
            w.opponent * (2 * opponent.get(pair_key(x, y), 0) + 1)
            for x in pair_a
            for y in pair_b
        )
        # 優先度5a: 対等なペア同士のマッチがよい。左右の強さの差に比例して減点する。
        cost += w.strength_gap * abs(self.pair_strength[pair_a] - self.pair_strength[pair_b])
        # 優先度6: 男女ペア同士のマッチが望ましい。
        cost += gender_cost(self.pair_kind[pair_a], self.pair_kind[pair_b], w)
        # スキップされた試合をそのまま出さない。
        if key in self._avoid_matches:
            cost += w.avoid_match

        self._match_cost_cache[key] = cost
        return cost

    def group_cost(self, group: Group) -> tuple[int, tuple[Pair, Pair]]:
        """4人を1試合にしたときの最小コストと、そのときのペア分け。

        ペアの分け方は3通りしかないので、ここで決めてしまってよい。
        どう分けても他の試合には影響しないため、全体の最適解を損なわない。
        """
        cached = self._group_cache.get(group)
        if cached is not None:
            return cached

        best_cost: int | None = None
        best_pairing: tuple[Pair, Pair] | None = None
        ties = 0
        for (i, j), (k, m) in PAIRINGS_OF_FOUR:
            # group の並びは制約の強い順なので、キーはここで正規化する。
            pair_a: Pair = pair_key(group[i], group[j])
            pair_b: Pair = pair_key(group[k], group[m])
            cost = self.pair_cost[pair_a] + self.pair_cost[pair_b]
            cost += self.match_cost(pair_a, pair_b)
            # 優先度5: 初心者を含むペア同士でマッチを組む。
            # 「片側だけに初心者がいる試合」に減点することで、初心者2名を
            # 同じ試合の対面に置く編成が、1名だけ出す編成より良いと評価される。
            if self.pair_has_beginner[pair_a] + self.pair_has_beginner[pair_b] == 1:
                cost += self._weights.beginner_concentration

            if best_cost is None or cost < best_cost:
                best_cost, best_pairing, ties = cost, (pair_a, pair_b), 1
            elif cost == best_cost:
                ties += 1
                if self._rng.randrange(ties) == 0:
                    best_pairing = (pair_a, pair_b)

        assert best_cost is not None and best_pairing is not None
        result = (best_cost, best_pairing)
        self._group_cache[group] = result
        return result


# ---------------------------------------------------------------------------
# 候補の列挙
# ---------------------------------------------------------------------------


def _split_candidates(
    active: Sequence[PlayerStat],
    state: _State,
    n_slots: int,
    fairness_slack: int,
) -> tuple[list[PlayerStat], list[PlayerStat], int]:
    """出場者候補を「確定」と「入れ替え可能」に分ける。

    ``adjusted`` が閾値未満の人は必ず出す。閾値ちょうどの人が入れ替え可能枠になる。
    ``fairness_slack`` を 1 以上にすると、1試合ぶん多く出ている人も候補に含める。
    既定は 0（厳密公平）。参加回数の均等性は仕様で優先度が高いうえ、実測では
    枠を広げてもペアのばらけ方は改善しなかったため。
    """
    ordered = sorted(active, key=lambda p: state.adjusted[p.id])
    threshold = state.adjusted[ordered[n_slots - 1].id]
    must = [p for p in active if state.adjusted[p.id] < threshold]
    flexible = [
        p for p in active if threshold <= state.adjusted[p.id] <= threshold + fairness_slack
    ]
    return must, flexible, math.comb(len(flexible), n_slots - len(must))


def _candidate_sets(
    must: Sequence[PlayerStat],
    flexible: Sequence[PlayerStat],
    need: int,
    total: int,
    *,
    limit: int,
    rng: random.Random,
) -> list[tuple[PlayerStat, ...]]:
    """評価する出場者集合を列挙する。多すぎる場合はサンプリングする。"""
    if total <= limit:
        return [(*must, *combo) for combo in combinations(flexible, need)]

    seen: set[tuple[int, ...]] = set()
    sets: list[tuple[PlayerStat, ...]] = []
    for _ in range(limit * 20):
        if len(sets) >= limit:
            break
        picked = tuple(sorted(rng.sample(range(len(flexible)), need)))
        if picked in seen:
            continue
        seen.add(picked)
        sets.append((*must, *(flexible[i] for i in picked)))
    return sets


def _keyed_candidates(
    indexes: Sequence[int],
    costs: Sequence[int],
    parents: Sequence[int],
    groups: Sequence[Group],
    masks: Sequence[int],
    batches: Sequence[Batch],
    scorer: _Scorer,
) -> list[tuple[int, int, Batch, int]]:
    """指定した候補を (コスト, tie_break, 組み分け, 使用済みビット) にする。"""
    keyed = []
    for k in indexes:
        batch: Batch = (*batches[parents[k]], groups[k])
        keyed.append((costs[k], scorer.tie_break(batch), batch, masks[k]))
    return keyed


def split_into_matches(
    ids: Sequence[int],
    n_matches: int,
    scorer: _Scorer,
    *,
    beam_width: int | None = None,
) -> list[tuple[int, Batch]]:
    """出場者を n_matches 個の4人組に分ける。安い順に返す。

    「まだ使っていない中で先頭の人」を必ず次の組に入れることで、
    同じ分け方を数え直さずに済む。それでも全通りは 8人で35、12人で5,775、
    16人で262万と増えるので、1試合決めるごとに安い順へ刈り込む。
    8人（2面）なら35通りすべてが残るため、よく使う構成では厳密な最小解になる。

    候補の大半は刈り込みで捨てるので、捨てる候補には組み分けのタプルも
    同点ほぐしのハッシュも作らない。16人4面では1回の生成で約130万件の候補を作り、
    残すのは各段 ``beam_width`` 件だけ。結果は、全候補に (コスト, tie_break,
    組み分け, 使用済みビット) を付けて小さい順に取るのと完全に同じになる。
    """
    # 既定値を引数に書くと定義時に束縛され、定数を差し替えても効かない。
    beam_width = BATCH_BEAM if beam_width is None else beam_width
    total = len(ids)
    bits = [1 << i for i in range(total)]
    group_cache = scorer._group_cache
    states: list[tuple[int, Batch, int]] = [(0, (), 0)]  # コスト, 組み分け, 使用済みビット

    for level in range(n_matches):
        # 各段の候補数は、残す案の数 x 残りの人から先頭以外の3人を選ぶ組合せ数。
        remaining = total - level * PLAYERS_PER_MATCH
        n_candidates = len(states) * math.comb(remaining - 1, PLAYERS_PER_MATCH - 1)
        if n_candidates <= beam_width:
            # 刈り込まない段（8人2面など）。候補をそのまま状態にする。
            nxt: list[tuple[int, Batch, int]] = []
            for cost, batch, used in states:
                first = next(i for i in range(total) if not used & bits[i])
                rest = [i for i in range(first + 1, total) if not used & bits[i]]
                head = ids[first]
                base = used | bits[first]
                for a, b, c in combinations(rest, PLAYERS_PER_MATCH - 1):
                    group: Group = (head, ids[a], ids[b], ids[c])
                    cached = group_cache.get(group)
                    group_cost = cached[0] if cached is not None else scorer.group_cost(group)[0]
                    mask = base | bits[a] | bits[b] | bits[c]
                    nxt.append((cost + group_cost, (*batch, group), mask))
            states = nxt
            continue

        # 刈り込む段。候補は列ごとに持ち、組み分けのタプルは残すと決まった候補にだけ作る。
        costs: list[int] = []
        parents: list[int] = []
        groups: list[Group] = []
        masks: list[int] = []
        for parent, (cost, _batch, used) in enumerate(states):
            first = next(i for i in range(total) if not used & bits[i])
            rest = [i for i in range(first + 1, total) if not used & bits[i]]
            head = ids[first]
            base = used | bits[first]
            for a, b, c in combinations(rest, PLAYERS_PER_MATCH - 1):
                group = (head, ids[a], ids[b], ids[c])
                cached = group_cache.get(group)
                group_cost = cached[0] if cached is not None else scorer.group_cost(group)[0]
                costs.append(cost + group_cost)
                parents.append(parent)
                groups.append(group)
                masks.append(base | bits[a] | bits[b] | bits[c])

        batches = [batch for _cost, batch, _used in states]
        # 境目のコスト（安い方から beam_width 番目）より安い候補は必ず残る。
        # 境目と同点の候補だけを tie_break で選ぶ。
        # キーを挟まないと、同点はタプルの次の要素＝member_id の辞書順で
        # 決まり、id の小さい組ばかりが生き残る（不変則9/10）。
        cutoff = heapq.nsmallest(beam_width, costs)[-1]
        below = [k for k, cost in enumerate(costs) if cost < cutoff]
        tied = [k for k, cost in enumerate(costs) if cost == cutoff]
        kept = _keyed_candidates(below, costs, parents, groups, masks, batches, scorer)
        kept += heapq.nsmallest(
            beam_width - len(kept),
            _keyed_candidates(tied, costs, parents, groups, masks, batches, scorer),
        )
        kept.sort()
        states = [(cost, batch, mask) for cost, _key, batch, mask in kept]

    return [(cost, batch) for cost, batch, _used in states]


# ---------------------------------------------------------------------------
# 先読み（ロールアウト）
# ---------------------------------------------------------------------------

Candidate = tuple[int, Batch]
"""(スコア, 組み分け)。"""


def _pairings_of(batch: Batch, scorer: _Scorer) -> list[tuple[Pair, Pair]]:
    return [scorer.group_cost(group)[1] for group in batch]


def _round_signature(batch: Batch, scorer: _Scorer) -> tuple:
    """コートの入れ替えを無視した、ラウンド全体の編成の署名。"""
    return tuple(sorted(tuple(sorted(pairing)) for pairing in _pairings_of(batch, scorer)))


def _plan_round(
    active: Sequence[PlayerStat],
    state: _State,
    weights: Weights,
    *,
    n_matches: int,
    fairness_slack: int,
    max_candidate_sets: int,
    rng: random.Random,
    keep: int,
    tie_salt: int,
    batch_beam: int | None = None,
    avoid_rounds: frozenset = frozenset(),
    avoid_matches: frozenset = frozenset(),
) -> tuple[list[Candidate], _Scorer]:
    """1ラウンド分の組み分けを、安い順に ``keep`` 件返す。"""
    n_slots = n_matches * PLAYERS_PER_MATCH
    must, flexible, total = _split_candidates(active, state, n_slots, fairness_slack)
    candidate_sets = _candidate_sets(
        must,
        flexible,
        n_slots - len(must),
        total,
        limit=max_candidate_sets,
        rng=rng,
    )

    scorer = _Scorer(
        active, state, weights, rng=rng, avoid_matches=avoid_matches, tie_salt=tie_salt
    )
    costs = player_costs(active, state, weights)
    baseline = benched_baseline(active, state, weights)

    # 組を決める順番。制約の強い人（初心者 → ルール未習得 → その他）を先に置く。
    # 探索は「まだ使っていない中で先頭の人」を必ず次の組に入れるので、後ろに
    # 置かれた人ほど選択肢が残らない。初心者が最後に固まると、避けようのない
    # 初心者同士ペアができてしまう（仕様3a）。
    #
    # 同じ制約どうしは id 順のまま並べる。ここを変えると、レベル差が無い
    # 練習会でも探索の道筋が変わり、ばらけ（優先度1）が落ちる。
    # id 順に並べても偏りは出ない。刈り込みの同点は tie_break が決めるので、
    # 「id の小さい組だけが生き残る」ことはない（不変則9/10）。
    rank = {p.id: (0 if p.is_beginner else 1 if not p.knows_rules else 2) for p in active}

    scored: list[tuple[int, int, Batch]] = []
    for selected in candidate_sets:
        ids = tuple(sorted((p.id for p in selected), key=lambda i: (rank[i], i)))
        base = baseline + sum(costs[member_id] for member_id in ids)
        for cost, batch in split_into_matches(
            ids, n_matches, scorer, beam_width=batch_beam
        ):
            score = base + cost
            if avoid_rounds and _round_signature(batch, scorer) in avoid_rounds:
                score += weights.avoid_round
            # 同点は先頭を採らない。並べ替えのキーに乱数を混ぜて一様抽選にする。
            scored.append((score, rng.getrandbits(32), batch))

    best = heapq.nsmallest(keep, scored)
    return [(score, batch) for score, _key, batch in best], scorer


def _rollout_cost(
    candidate: Candidate,
    scorer: _Scorer,
    active: Sequence[PlayerStat],
    state: _State,
    weights: Weights,
    *,
    n_matches: int,
    fairness_slack: int,
    depth: int,
    seed: int,
    active_ids: tuple[int, ...],
    beginners: frozenset[int],
) -> int:
    """この候補を採ったとして、その先 ``depth`` ラウンドまでの累積コスト。

    1ラウンドだけを見ると良くても、その先で組める相手が減ってしまう選択がある。
    先の手を貪欲に打ってみて、累積で比べることでそれを避ける。

    先読みの乱数は候補ごとに同じ種から作る。同じ運の下で比べるため
    （共通乱数法）で、候補間の差が乱数のぶれに埋もれないようにする。
    """
    score, batch = candidate
    rollout_state = state.copy()
    rollout_state.apply(_pairings_of(batch, scorer), active_ids, beginners)

    total = score
    rng = random.Random(seed)
    for _ in range(depth):
        following, next_scorer = _plan_round(
            active,
            rollout_state,
            weights,
            n_matches=n_matches,
            fairness_slack=fairness_slack,
            max_candidate_sets=ROLLOUT_CANDIDATE_SETS,
            rng=rng,
            keep=1,
            tie_salt=seed,
            batch_beam=ROLLOUT_BATCH_BEAM,
        )
        if not following:
            break
        next_score, next_batch = following[0]
        total += next_score
        rollout_state.apply(_pairings_of(next_batch, next_scorer), active_ids, beginners)
    return total


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def _build_round_plan(
    batch: Batch,
    scorer: _Scorer,
    benched: Sequence[PlayerStat],
    resting: Sequence[PlayerStat],
    *,
    court_count: int,
    score: int,
    rng: random.Random,
) -> RoundPlan:
    """組み分けをコートに流し込んで RoundPlan にする。

    使うコートは先頭から順に埋める（会場で迷わせないため）。
    どの試合をどのコートに置くか、チームの左右、ペア内の並びはシャッフルする
    （強い人がいつも左、登録が早い人がいつも先頭、を防ぐ）。
    """
    pairings = _pairings_of(batch, scorer)
    if len(pairings) > 1:
        rng.shuffle(pairings)

    plans: list[MatchPlan] = []
    playing: list[int] = []
    for court_index, (pair_a, pair_b) in enumerate(pairings):
        teams = [list(pair_a), list(pair_b)]
        if rng.random() < 0.5:
            teams.reverse()
        for team in teams:
            if rng.random() < 0.5:
                team.reverse()
        team_a = (teams[0][0], teams[0][1])
        team_b = (teams[1][0], teams[1][1])
        plans.append(MatchPlan(court_index=court_index, team_a=team_a, team_b=team_b))
        playing.extend(team_a)
        playing.extend(team_b)

    return RoundPlan(
        court_count=court_count,
        matches=tuple(plans),
        playing=tuple(playing),
        sitting_out=tuple(p.id for p in benched),
        resting=tuple(p.id for p in resting),
        score=score,
    )


def generate_round(
    players: Sequence[PlayerStat],
    history: History,
    *,
    court_count: int,
    seed: int,
    rng: random.Random,
    weights: Weights | None = None,
    fairness_slack: int = 0,
    max_candidate_sets: int = 60,
    lookahead: int = 1,
    beam: int = 16,
    avoid: Sequence[tuple] = (),
) -> RoundPlan:
    """次のラウンドの対戦カードを1つ作る。

    Args:
        players: 練習会のメンバー全員（``LEFT`` は無視される）。
        history: これまでの採用ラウンドから導出した履歴。
        court_count: 試合に使えるコート数。人数が足りなければ一部は使わない。
        seed: 練習会の乱数シード。並び順の正規化に使う。
        rng: 生成1回分の乱数生成器。:func:`make_rng` で作る。
        weights: スコアの重み。
        fairness_slack: 出場回数が最少の人より何試合分まで多く出ている人を
            候補に含めてよいか。既定の 0 は厳密公平。
        max_candidate_sets: 評価する出場者集合の上限。
        lookahead: 何ラウンド先まで読むか。0 なら1ラウンドだけを見る貪欲法。
            既定の 1 で、ペアと対戦の重複の理論超過が貪欲法より約1割少なくなる。
        beam: 先読みで比べる上位候補の数。
        avoid: 避けたい編成の署名（``RoundPlan.signature()``）。スキップ時に渡す。

    Raises:
        NotEnoughPlayersError: 出場可能なメンバーが4人未満のとき。
    """
    weights = weights or Weights()
    normalized = normalize_players(players, seed)
    active = [p for p in normalized if p.status is MemberStatus.ACTIVE]
    resting = [p for p in normalized if p.status is MemberStatus.RESTING]

    if court_count < 1:
        # 呼び出し側が先に弾くが、人数のせいだと誤解させる文面は出さない。
        raise NotEnoughPlayersError("試合に使えるコートがありません")
    n_matches = min(court_count, len(active) // PLAYERS_PER_MATCH)
    if n_matches < 1:
        raise NotEnoughPlayersError(
            f"マッチを組むには4人以上必要です（出場可能なメンバーは{len(active)}人）"
        )

    # コート数が増えると1ラウンドの組み分けが一気に増えるので、
    # 探索の幅を絞って所要時間を抑える。よく使う2面までは絞らない。
    spread = max(1, (n_matches - 1) ** 2)
    effective_sets = max(4, max_candidate_sets // spread)
    effective_beam = max(4, beam // max(1, n_matches - 1))

    state = _State.from_history(active, history)
    avoid_rounds = frozenset(tuple(sig) for sig in avoid)
    avoid_matches = frozenset(match_sig for sig in avoid for match_sig in sig)

    candidates, scorer = _plan_round(
        active,
        state,
        weights,
        n_matches=n_matches,
        fairness_slack=fairness_slack,
        max_candidate_sets=effective_sets,
        rng=rng,
        keep=effective_beam if lookahead > 0 else 1,
        tie_salt=seed,
        avoid_rounds=avoid_rounds,
        avoid_matches=avoid_matches,
    )

    if lookahead > 0 and len(candidates) > 1:
        active_ids = tuple(p.id for p in active)
        beginners = frozenset(p.id for p in active if p.is_beginner)
        rollout_seed = rng.getrandbits(63)
        chosen = min(
            range(len(candidates)),
            key=lambda i: (
                _rollout_cost(
                    candidates[i],
                    scorer,
                    active,
                    state,
                    weights,
                    n_matches=n_matches,
                    fairness_slack=fairness_slack,
                    depth=lookahead,
                    seed=rollout_seed,
                    active_ids=active_ids,
                    beginners=beginners,
                ),
                i,
            ),
        )
        score, batch = candidates[chosen]
    else:
        score, batch = candidates[0]

    selected_ids = {member_id for group in batch for member_id in group}
    benched = [p for p in active if p.id not in selected_ids]

    return _build_round_plan(
        batch,
        scorer,
        benched,
        resting,
        court_count=court_count,
        score=score,
        rng=rng,
    )
