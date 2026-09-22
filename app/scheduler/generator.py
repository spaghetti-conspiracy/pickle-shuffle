"""対戦カードの生成。

設計の要点:

* スコアは「選手項 + ペア項 + 試合項 + ラウンド項」に分解できるので、
  出場者の組合せごとに編成を全列挙しても十分速い。近似は使わない。
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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cache
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
"""ペア。出場者を id の昇順に並べたときの添字2つ、または member_id 2つ。"""

Arrangement = tuple[tuple[Pair, Pair], ...]
"""1ラウンドの編成。試合ごとに (ペア, ペア)。"""


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


# ---------------------------------------------------------------------------
# 編成の列挙
# ---------------------------------------------------------------------------


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


def _pairings(members: tuple[int, ...]) -> Iterator[tuple[Pair, ...]]:
    """メンバーをペアに分割する全パターン。8人なら 105 通り。"""
    if not members:
        yield ()
        return
    first, rest = members[0], members[1:]
    for i, partner in enumerate(rest):
        remaining = rest[:i] + rest[i + 1 :]
        head = pair_key(first, partner)
        for tail in _pairings(remaining):
            yield (head, *tail)


def _group_into_matches(pairs: tuple[Pair, ...]) -> Iterator[Arrangement]:
    """ペアを2つずつ組にして試合にする全パターン。4ペアなら 3 通り。"""
    if not pairs:
        yield ()
        return
    first, rest = pairs[0], pairs[1:]
    for i, opponent in enumerate(rest):
        remaining = rest[:i] + rest[i + 1 :]
        for tail in _group_into_matches(remaining):
            yield ((first, opponent), *tail)


@cache
def index_arrangements(n: int) -> tuple[Arrangement, ...]:
    """位置(0〜n-1)に対する編成の全パターン。8人なら 105 x 3 = 315 通り。

    人数が同じなら形は同じなので使い回す。出場者の id を昇順に並べてから
    この添字を当てれば、ペアは常に昇順になり、正規化のためのソートが要らない。
    """
    arrangements = []
    for pairs in _pairings(tuple(range(n))):
        arrangements.extend(_group_into_matches(pairs))
    return tuple(arrangements)


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
        arrangement: Arrangement,
        playing: frozenset[int],
        active_ids: Sequence[int],
        beginners: frozenset[int],
    ) -> None:
        """1ラウンドぶん進める。"""
        for pair_a, pair_b in arrangement:
            for team in (pair_a, pair_b):
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


class _Scorer:
    """ペア単位・試合単位のコストを計算する。

    これらのコストは「誰が出るか」には依存せず、履歴と2人（または2ペア）だけで決まる。
    そのため1ラウンドの探索につき1度だけ作り、すべての候補集合で使い回す。
    """

    def __init__(
        self,
        players: Sequence[PlayerStat],
        state: _State,
        weights: Weights,
    ) -> None:
        self._state = state
        self._weights = weights
        self._match_cost_cache: dict[tuple[Pair, Pair], int] = {}

        self.pair_cost: dict[Pair, int] = {}
        self.pair_kind: dict[Pair, PairKind] = {}
        self.pair_has_beginner: dict[Pair, int] = {}

        for a, b in combinations(players, 2):
            key = pair_key(a.id, b.id)
            self.pair_kind[key] = _pair_kind(a, b)
            self.pair_has_beginner[key] = int(a.is_beginner or b.is_beginner)
            self.pair_cost[key] = self._compute_pair_cost(a, b, key)

    def _compute_pair_cost(self, a: PlayerStat, b: PlayerStat, key: Pair) -> int:
        w = self._weights
        # 優先度1: 同じ相手とばかり組まないようにする。
        # 増分 2n+1 は「ペアを組んだ回数の二乗和」を最小化する = 回数を均す。
        cost = w.partner * (2 * self._state.partner.get(key, 0) + 1)

        if a.is_beginner and b.is_beginner:
            # 優先度3: 初心者同士のペアは避ける。実質ハード制約。
            cost += w.beginner_pair
        elif a.is_beginner or b.is_beginner:
            # 優先度4: 初心者と組む回数を、非初心者の間で均等にする。
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
        # 優先度6: 男女ペア同士のマッチが望ましい。
        cost += gender_cost(self.pair_kind[pair_a], self.pair_kind[pair_b], w)

        self._match_cost_cache[key] = cost
        return cost


def _selection_cost(
    selected: Sequence[PlayerStat],
    benched: Sequence[PlayerStat],
    state: _State,
    weights: Weights,
) -> int:
    """誰を出すかだけで決まるコスト。編成によらない。"""
    w = weights
    # 優先度2: 参加回数の公平性。
    # ラウンドで増える出場数は固定なので、出場後の adjusted の分散を最小化することは
    # 「出場者の (2 * adjusted + 1) の和」の最小化と等価になる。
    cost = sum(w.fair * (2 * state.adjusted[p.id] + 1) for p in selected)
    # 優先度3: 連続してマッチに入れない回数を最小にする。二乗で強く効かせる。
    cost += sum(w.sit_out * (state.sit_out_streak[p.id] + 1) ** 2 for p in benched)
    # 優先度7: 休み明けはなるべく早く入れる。効き目は小さくてよい。
    cost += sum(w.just_returned for p in benched if p.id in state.just_returned)
    return cost


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


Candidate = tuple[int, tuple[int, ...], Arrangement]
"""(スコア, 出場者idの昇順タプル, 添字による編成)。"""


def _best_candidates(
    active: Sequence[PlayerStat],
    state: _State,
    weights: Weights,
    *,
    n_slots: int,
    fairness_slack: int,
    max_candidate_sets: int,
    rng: random.Random,
    avoid_rounds: frozenset,
    avoid_matches: frozenset,
    keep: int,
) -> list[Candidate]:
    """スコアの小さい順に候補を ``keep`` 件返す。

    同点のときに先頭を採らないよう、並べ替えのキーに乱数を混ぜる。
    こうすると同点集合からの一様抽選になり、かつ上位候補も偏りなく集まる。
    """
    must, flexible, total = _split_candidates(active, state, n_slots, fairness_slack)
    candidate_sets = _candidate_sets(
        must,
        flexible,
        n_slots - len(must),
        total,
        limit=max_candidate_sets,
        rng=rng,
    )

    scorer = _Scorer(active, state, weights)
    arrangements = index_arrangements(n_slots)
    check_avoid = bool(avoid_rounds or avoid_matches)
    beginner_concentration = weights.beginner_concentration

    scored: list[tuple[int, int, tuple[int, ...], Arrangement]] = []
    for selected in candidate_sets:
        selected_ids = {p.id for p in selected}
        benched = [p for p in active if p.id not in selected_ids]
        base = _selection_cost(selected, benched, state, weights)

        # 出場者を id 昇順に並べ、添字ペア -> コストの表を作る。
        # 以降の内側ループは小さな辞書引きと整数演算だけになる。
        ids = tuple(sorted(selected_ids))
        pair_cost: dict[Pair, int] = {}
        has_beginner: dict[Pair, int] = {}
        for i in range(n_slots):
            for j in range(i + 1, n_slots):
                key = (ids[i], ids[j])
                pair_cost[(i, j)] = scorer.pair_cost[key]
                has_beginner[(i, j)] = scorer.pair_has_beginner[key]
        match_cost: dict[tuple[Pair, Pair], int] = {}

        for arrangement in arrangements:
            score = base
            lonely_beginner_matches = 0
            for index_pair_a, index_pair_b in arrangement:
                score += pair_cost[index_pair_a] + pair_cost[index_pair_b]
                cached = match_cost.get((index_pair_a, index_pair_b))
                if cached is None:
                    cached = scorer.match_cost(
                        (ids[index_pair_a[0]], ids[index_pair_a[1]]),
                        (ids[index_pair_b[0]], ids[index_pair_b[1]]),
                    )
                    match_cost[(index_pair_a, index_pair_b)] = cached
                score += cached
                if has_beginner[index_pair_a] + has_beginner[index_pair_b] == 1:
                    lonely_beginner_matches += 1
            # 優先度5: 初心者を含むペア同士でマッチを組む。
            # 「片側だけに初心者がいる試合」を数えることで、初心者2名を同じ試合の
            # 対面に置く編成が、初心者1名だけを出す編成より良いと評価される。
            score += beginner_concentration * lonely_beginner_matches

            if check_avoid:
                # スキップされた編成を繰り返さない。
                match_sigs = _match_signatures(ids, arrangement)
                if match_sigs in avoid_rounds:
                    score += weights.avoid_round
                score += weights.avoid_match * sum(1 for sig in match_sigs if sig in avoid_matches)

            scored.append((score, rng.getrandbits(32), ids, arrangement))

    best = heapq.nsmallest(keep, scored)
    return [(score, ids, arrangement) for score, _key, ids, arrangement in best]


def _match_signatures(ids: tuple[int, ...], arrangement: Arrangement) -> tuple:
    """コートの入れ替えを無視した、試合ごとの署名。"""
    return tuple(
        sorted(
            tuple(sorted(((ids[a], ids[b]), (ids[c], ids[d]))))
            for (a, b), (c, d) in arrangement
        )
    )


# ---------------------------------------------------------------------------
# 先読み（ロールアウト）
# ---------------------------------------------------------------------------


def _rollout_cost(
    candidate: Candidate,
    active: Sequence[PlayerStat],
    state: _State,
    weights: Weights,
    *,
    n_slots: int,
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
    score, ids, arrangement = candidate
    rollout_state = state.copy()
    playing = frozenset(ids)
    arrangement_ids = tuple(
        ((ids[a], ids[b]), (ids[c], ids[d])) for (a, b), (c, d) in arrangement
    )
    rollout_state.apply(arrangement_ids, playing, active_ids, beginners)

    total = score
    rng = random.Random(seed)
    for _ in range(depth):
        following = _best_candidates(
            active,
            rollout_state,
            weights,
            n_slots=n_slots,
            fairness_slack=fairness_slack,
            max_candidate_sets=ROLLOUT_CANDIDATE_SETS,
            rng=rng,
            avoid_rounds=frozenset(),
            avoid_matches=frozenset(),
            keep=1,
        )
        if not following:
            break
        next_score, next_ids, next_arrangement = following[0]
        total += next_score
        next_pairs = tuple(
            ((next_ids[a], next_ids[b]), (next_ids[c], next_ids[d]))
            for (a, b), (c, d) in next_arrangement
        )
        rollout_state.apply(next_pairs, frozenset(next_ids), active_ids, beginners)
    return total


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def _build_round_plan(
    arrangement: Arrangement,
    ids: tuple[int, ...],
    benched: Sequence[PlayerStat],
    resting: Sequence[PlayerStat],
    *,
    court_count: int,
    score: int,
    rng: random.Random,
) -> RoundPlan:
    """採用した編成を RoundPlan に変換する。

    使うコートは court_index の小さい方から詰める（会場で迷わせないため）。
    どの編成をどのコートに置くか、チームの左右、ペア内の並びはシャッフルする
    （強い人がいつも左、登録が早い人がいつも先頭、を防ぐ）。
    """
    matches = [((ids[a], ids[b]), (ids[c], ids[d])) for (a, b), (c, d) in arrangement]
    if len(matches) > 1:
        rng.shuffle(matches)

    plans: list[MatchPlan] = []
    playing: list[int] = []
    for court_index, (pair_a, pair_b) in enumerate(matches):
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
        court_count: 練習会が持つコート数。人数が足りなければ一部は使わない。
        seed: 練習会の乱数シード。並び順の正規化に使う。
        rng: 生成1回分の乱数生成器。:func:`make_rng` で作る。
        weights: スコアの重み。
        fairness_slack: 出場回数が最少の人より何試合分まで多く出ている人を
            候補に含めてよいか。既定の 0 は厳密公平。
        max_candidate_sets: 評価する出場者集合の上限。
        lookahead: 何ラウンド先まで読むか。0 なら1ラウンドだけを見る貪欲法。
            既定の 1 で、ペアと対戦の重複の理論超過が貪欲法より約4割少なくなる。
            2 以上に深くしても改善しない（先の手の読みが粗いため）。
        beam: 先読みで比べる上位候補の数。16 より増やしても改善しなかった。
        avoid: 避けたい編成の署名（``RoundPlan.signature()``）。スキップ時に渡す。

    Raises:
        NotEnoughPlayersError: 出場可能なメンバーが4人未満のとき。
    """
    weights = weights or Weights()
    normalized = normalize_players(players, seed)
    active = [p for p in normalized if p.status is MemberStatus.ACTIVE]
    resting = [p for p in normalized if p.status is MemberStatus.RESTING]

    used_courts = min(court_count, len(active) // PLAYERS_PER_MATCH)
    if used_courts < 1:
        raise NotEnoughPlayersError(
            f"マッチを組むには4人以上必要です（出場可能なメンバーは{len(active)}人）"
        )
    n_slots = used_courts * PLAYERS_PER_MATCH

    state = _State.from_history(active, history)
    avoid_rounds = frozenset(tuple(sig) for sig in avoid)
    avoid_matches = frozenset(match_sig for sig in avoid for match_sig in sig)

    candidates = _best_candidates(
        active,
        state,
        weights,
        n_slots=n_slots,
        fairness_slack=fairness_slack,
        max_candidate_sets=max_candidate_sets,
        rng=rng,
        avoid_rounds=avoid_rounds,
        avoid_matches=avoid_matches,
        keep=beam if lookahead > 0 else 1,
    )

    if lookahead > 0 and len(candidates) > 1:
        active_ids = tuple(p.id for p in active)
        beginners = frozenset(p.id for p in active if p.is_beginner)
        rollout_seed = rng.getrandbits(63)
        best_index = min(
            range(len(candidates)),
            key=lambda i: (
                _rollout_cost(
                    candidates[i],
                    active,
                    state,
                    weights,
                    n_slots=n_slots,
                    fairness_slack=fairness_slack,
                    depth=lookahead,
                    seed=rollout_seed,
                    active_ids=active_ids,
                    beginners=beginners,
                ),
                i,
            ),
        )
        chosen = candidates[best_index]
    else:
        chosen = candidates[0]

    score, ids, arrangement = chosen
    selected_ids = set(ids)
    benched = [p for p in active if p.id not in selected_ids]

    return _build_round_plan(
        arrangement,
        ids,
        benched,
        resting,
        court_count=court_count,
        score=score,
        rng=rng,
    )
