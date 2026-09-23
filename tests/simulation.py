"""練習会の進行をシミュレートするテスト用ヘルパ。

統計の導出には実装と同じ :mod:`app.scheduler.stats_rules` を使う。
ここで独自に参加回数を数え直すと、導出規則の誤りを見逃してしまうため。
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass

from app.scheduler.domain import (
    Gender,
    History,
    Level,
    MemberStatus,
    ParticipationState,
    PlayerStat,
    RoundPlan,
    Weights,
    pair_key,
)
from app.scheduler.generator import generate_round, make_rng
from app.scheduler.stats_rules import derive


@dataclass
class MemberSpec:
    """テストで作るメンバーの指定。"""

    id: int
    nickname: str
    gender: Gender = Gender.MALE
    level: Level = Level.PICKLEBALL


def make_members(
    count: int,
    *,
    males: int | None = None,
    beginners: int = 0,
    racket: int = 0,
    start_id: int = 1,
    beginners_last: bool = False,
) -> list[MemberSpec]:
    """テスト用のメンバー一覧を作る。

    Args:
        count: 人数。
        males: 男性の人数。``None`` なら男女交互。
        beginners: 何人を初心者にするか。
        racket: 初心者に続けて何人をラケット経験者（ルール習得中）にするか。
        start_id: id の開始値。
        beginners_last: 初心者を末尾（id の大きい方）に置く。

            初心者は公募で後から登録されることが多く、実際には id が大きく
            なりやすい。組み分けの探索は id 順を起点にしていた時期があり、
            初心者が末尾にいると最後の組に固まって初心者同士ペアができた。
            先頭に置いた構成だけでは、この欠陥を踏めない。
    """
    members: list[MemberSpec] = []
    for i in range(count):
        if males is None:
            gender = Gender.MALE if i % 2 == 0 else Gender.FEMALE
        else:
            gender = Gender.MALE if i < males else Gender.FEMALE
        # beginners_last なら末尾から数える。
        special = count - 1 - i if beginners_last else i
        if special < beginners:
            level = Level.BEGINNER
        elif special < beginners + racket:
            level = Level.RACKET_EXPERIENCED
        else:
            level = Level.PICKLEBALL
        members.append(
            MemberSpec(id=start_id + i, nickname=f"m{start_id + i}", gender=gender, level=level)
        )
    return members


class Simulator:
    """練習会1回分の進行を再現する。"""

    def __init__(
        self,
        members: list[MemberSpec],
        *,
        seed: int = 20260922,
        court_count: int = 2,
        weights: Weights | None = None,
        fairness_slack: int = 0,
        max_candidate_sets: int = 60,
        lookahead: int = 1,
        beam: int = 16,
    ) -> None:
        self.seed = seed
        self.court_count = court_count
        self.weights = weights or Weights()
        self.fairness_slack = fairness_slack
        self.lookahead = lookahead
        self.beam = beam
        self.max_candidate_sets = max_candidate_sets

        self.specs: dict[int, MemberSpec] = {m.id: m for m in members}
        self.status: dict[int, MemberStatus] = {m.id: MemberStatus.ACTIVE for m in members}
        self.baseline: dict[int, int] = {m.id: 0 for m in members}
        self.states: dict[int, list[ParticipationState]] = {m.id: [] for m in members}
        self.history = History()
        self.adopted_rounds = 0
        self.attempt = 0
        self.rejected_signatures: list[tuple] = []
        self.adopted_plans: list[RoundPlan] = []

    # ----- メンバーの操作 -------------------------------------------------

    def add_member(self, spec: MemberSpec) -> None:
        """途中から参加するメンバーを追加する。

        下駄(baseline)は、参加時点の active メンバーの最小 adjusted。
        これが無いと、途中参加者が何ラウンドも連続で出場してしまう。
        """
        actives = [p for p in self.player_stats() if p.status is MemberStatus.ACTIVE]
        self.specs[spec.id] = spec
        self.status[spec.id] = MemberStatus.ACTIVE
        self.states[spec.id] = []
        self.baseline[spec.id] = min((p.adjusted for p in actives), default=0)

    def set_status(self, member_id: int, status: MemberStatus) -> None:
        """休憩・復帰・離脱。"""
        self.status[member_id] = status

    # ----- 統計 -----------------------------------------------------------

    def player_stats(self) -> list[PlayerStat]:
        """現時点の PlayerStat 一覧。"""
        stats: list[PlayerStat] = []
        for member_id, spec in self.specs.items():
            status = self.status[member_id]
            if status is MemberStatus.LEFT:
                continue
            derived = derive(self.states[member_id], status)
            stats.append(
                PlayerStat(
                    id=member_id,
                    nickname=spec.nickname,
                    gender=spec.gender,
                    level=spec.level,
                    baseline=self.baseline[member_id],
                    plays=derived.plays,
                    rest_credit=derived.rest_credit,
                    sit_out_streak=derived.sit_out_streak,
                    just_returned=derived.just_returned,
                    status=status,
                )
            )
        return stats

    def stat(self, member_id: int) -> PlayerStat:
        """1人分の PlayerStat。"""
        return next(p for p in self.player_stats() if p.id == member_id)

    def play_counts(self) -> dict[int, int]:
        """実際の出場回数。"""
        return {p.id: p.plays for p in self.player_stats()}

    # ----- ラウンド -------------------------------------------------------

    def generate(self, *, players: list[PlayerStat] | None = None, rng: random.Random | None = None
                 ) -> RoundPlan:
        """次のラウンドを生成する（採用はしない）。"""
        return generate_round(
            players if players is not None else self.player_stats(),
            self.history,
            court_count=self.court_count,
            seed=self.seed,
            rng=rng or make_rng(self.seed, self.adopted_rounds + 1, self.attempt),
            weights=self.weights,
            fairness_slack=self.fairness_slack,
            lookahead=self.lookahead,
            beam=self.beam,
            max_candidate_sets=self.max_candidate_sets,
            avoid=tuple(self.rejected_signatures),
        )

    def reject(self, plan: RoundPlan) -> None:
        """不採用にする。統計には一切影響しない。"""
        self.rejected_signatures.append(plan.signature())
        self.attempt += 1

    def adopt(self, plan: RoundPlan) -> None:
        """採用する。スナップショットを1行ずつ記録し、履歴を更新する。"""
        playing = set(plan.playing)
        for member_id, status in self.status.items():
            if status is MemberStatus.LEFT:
                continue
            if member_id in playing:
                state = ParticipationState.PLAYED
            elif status is MemberStatus.RESTING:
                state = ParticipationState.RESTING
            else:
                state = ParticipationState.SAT_OUT
            self.states[member_id].append(state)

        for match in plan.matches:
            self._record_match(match.team_a, match.team_b)

        self.adopted_rounds += 1
        self.attempt = 0
        self.rejected_signatures.clear()
        self.adopted_plans.append(plan)

    def _record_match(self, team_a: tuple[int, int], team_b: tuple[int, int]) -> None:
        partner = self.history.partner_count
        opponent = self.history.opponent_count
        beginner_partner = self.history.beginner_partner_count

        for team in (team_a, team_b):
            key = pair_key(*team)
            partner[key] = partner.get(key, 0) + 1
            first, second = (self.specs[team[0]], self.specs[team[1]])
            if first.level.is_beginner != second.level.is_beginner:
                non_beginner = second if first.level.is_beginner else first
                beginner_partner[non_beginner.id] = beginner_partner.get(non_beginner.id, 0) + 1

        for x in team_a:
            for y in team_b:
                key = pair_key(x, y)
                opponent[key] = opponent.get(key, 0) + 1

    def run(self, rounds: int) -> list[RoundPlan]:
        """指定ラウンド数ぶん、生成して採用する。"""
        plans = []
        for _ in range(rounds):
            plan = self.generate()
            self.adopt(plan)
            plans.append(plan)
        return plans

    # ----- 集計 -----------------------------------------------------------

    def partner_counts(self) -> dict[tuple[int, int], int]:
        """ペアを組んだ回数。"""
        return dict(self.history.partner_count)

    def max_sit_out_streak_seen(self) -> int:
        """これまでに観測された連続不参加の最大値。"""
        worst = 0
        for states in self.states.values():
            streak = 0
            for state in states:
                if state is ParticipationState.SAT_OUT:
                    streak += 1
                    worst = max(worst, streak)
                else:
                    streak = 0
        return worst

    def unaware_pair_counts(self) -> dict[str, int]:
        """ルールを覚えていない者どうしのペアが何回できたか。"""
        counts = {"beginner-beginner": 0, "beginner-racket": 0, "racket-racket": 0}
        for plan in self.adopted_plans:
            for match in plan.matches:
                for team in (match.team_a, match.team_b):
                    levels = sorted(self.specs[i].level.value for i in team)
                    if levels == ["beginner", "beginner"]:
                        counts["beginner-beginner"] += 1
                    elif levels == ["beginner", "racket_experienced"]:
                        counts["beginner-racket"] += 1
                    elif levels == ["racket_experienced", "racket_experienced"]:
                        counts["racket-racket"] += 1
        return counts

    def strength_gaps(self) -> list[int]:
        """各試合の、左右のペアの強さの差。"""
        by_id = {p.id: p for p in self.player_stats()}
        gaps = []
        for plan in self.adopted_plans:
            for match in plan.matches:
                sides = [
                    sum(by_id[i].strength for i in team)
                    for team in (match.team_a, match.team_b)
                ]
                gaps.append(abs(sides[0] - sides[1]))
        return gaps

    def gender_pattern_counts(self) -> dict[str, int]:
        """採用された試合の男女構成を数える。"""
        from app.scheduler.generator import _pair_kind

        counts: dict[str, int] = defaultdict(int)
        by_id = {p.id: p for p in self.player_stats()}
        for plan in self.adopted_plans:
            for match in plan.matches:
                kinds = []
                for team in (match.team_a, match.team_b):
                    kinds.append(_pair_kind(by_id[team[0]], by_id[team[1]]).value)
                counts["-".join(sorted(kinds))] += 1
        return dict(counts)
