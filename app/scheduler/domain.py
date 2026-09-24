"""生成ロジックが扱う値オブジェクト。SQLAlchemy / FastAPI に依存しない。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Gender(str, Enum):
    """性別の区分。``OTHER`` は「その他 / 未回答」。"""

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


class Level(str, Enum):
    """レベル区分。"""

    BEGINNER = "beginner"
    """ルールがわからないラケットスポーツ未経験者。

    一日やってもボールを返せるようにならないことが多く、ゲーム自体が成立しない。
    """

    RACKET_EXPERIENCED = "racket_experienced"
    """ルールがわからないラケットスポーツ経験者。

    1試合が終わるころにはボールを相手コートに返せるようになり、ゲームは一応成立する。
    慣れたと判断したら管理者がレベルを変更する。
    """

    PICKLEBALL = "pickleball"
    """ピックルボール経験者。ルールを知っている。"""

    @property
    def is_beginner(self) -> bool:
        """初心者（ラケットスポーツ未経験者）かどうか。"""
        return self is Level.BEGINNER

    @property
    def knows_rules(self) -> bool:
        """ルールを覚えているか。"""
        return self is Level.PICKLEBALL

    @property
    def strength(self) -> int:
        """ペアの強さを見積もるための点数。

        単純な大小は 初心者 < ラケット経験者 < ピックルボール経験者 だが、
        初心者とラケット経験者の差は、ラケット経験者とピックルボール経験者の差より
        ずっと大きい（3倍程度）。その比を整数で表すために 0 / 6 / 8 としてある
        （男女の不均衡1人分が 2 に相当する尺度）。
        """
        return _LEVEL_STRENGTH[self]


_LEVEL_STRENGTH = {
    Level.BEGINNER: 0,
    Level.RACKET_EXPERIENCED: 6,
    Level.PICKLEBALL: 8,
}

#: 性別による加点。男性の方がパワーがあるぶん、やや有利と見る。
#: 男女の不均衡1人分が、ラケット経験者とピックルボール経験者の差と同じくらい。
_GENDER_STRENGTH = {
    Gender.MALE: 2,
    Gender.FEMALE: 0,
    Gender.OTHER: 1,
}


class MemberStatus(str, Enum):
    """メンバーの現在の状態。"""

    ACTIVE = "active"
    """出場可能。"""

    RESTING = "resting"
    """休憩中。マッチには入れないが、休憩クレジットが付く。"""

    LEFT = "left"
    """離脱済み。以降の集計対象から外れる。"""


class ParticipationState(str, Enum):
    """採用ラウンド1回分の、あるメンバーの状態スナップショット。"""

    PLAYED = "played"
    """出場した。"""

    SAT_OUT = "sat_out"
    """出場可能だったが、このラウンドでは出番がなかった。"""

    RESTING = "resting"
    """休憩中だった。"""


class RoundStatus(str, Enum):
    """ラウンドの状態。"""

    PENDING = "pending"
    """生成済みで、採用も不採用も決まっていない。"""

    ADOPTED = "adopted"
    """採用された（=「開始」された）。統計に反映される。"""

    REJECTED = "rejected"
    """不採用（=「スキップ」）。統計には一切影響しない。"""


class PairKind(str, Enum):
    """ペアの男女構成。"""

    MM = "mm"
    """男子ペア。"""

    FF = "ff"
    """女子ペア。"""

    MX = "mx"
    """男女ペア。``OTHER`` を含むペアもここに入れて減点しない。"""


@dataclass(frozen=True)
class Weights:
    """生成スコアの重み。値が大きいほど、その項目を強く避ける。

    doc/spec.md の優先度に対応する。増分形 ``2 * count + 1`` は
    「二乗和の最小化」＝「回数の均等化」と等価になるように選んである。

    **優先度2（参加回数の公平）は重みではなくハード制約で担保する。**
    生成の入口で「出場回数が最も少ない層」に候補を絞り込んでから採点するので
    （`_split_candidates` を参照）、実質的にすべてより上の制約として効く。
    参加回数差が実測で常に 1 以内に収まるのはこの枠のおかげで、重み `fair` は
    枠を緩めた（`fairness_slack >= 1`）ときの選好にしか効かない。

    その枠の内側では、重み付き和が次の順になる（1試合ぶんの差分で比較する）。
        初心者ペアの集中(5) 700 > 早すぎるペア重複(1) 500 > 連続不参加(3) 360
        > ペア重複(1) 240 ≒ 同じ4人(1) 240 ≒ 初心者の相手の偏り(4) 240
        > 参加回数(2) 200 > 対戦重複(1) 120 ≒ 直近の同じ3人(1) 120
        > 男女組合せ(6) 60/120
    仕様の優先度は 1 > 2 > 3 > 4 > 5 なので、**5 と 3 が 1 より上に来ているのは
    仕様からの逸脱**。重み付き和は本質的にレキシコ順にならないため、実測で品質が
    最も良くなる配分を採っている（ユーザー承認済み。doc/spec.md に明記）。

    初心者同士ペアと男子ペア対女子ペアだけは桁を変えて実質ハード制約にしてある。

    値は実測で決めた。判断の基準にしたのは、ペアと対戦の重複回数の二乗和が
    「同じ総数を均等に配分したときの理論最小値」からどれだけ超過するかで、
    重みに依存しないため異なる配分どうしを公平に比べられる。
    加えて、当人が気づく「早すぎるペア重複」「同じカード」「同じ4人」の回数を見た。
    この配分での実測値は doc/algorithm.md の「9. 品質の測り方」にまとめてある
    （ここに写すと、重みを変えたときに片方だけ古くなるため）。
    """

    # 優先度1: ペア・対戦の組合せがばらけること。
    # 1ラウンドで作られるペアは4組、対戦は8組なので、この比なら両者の影響が釣り合う。
    # 1件あたりの重みはペアの方が2倍（組む相手の方が強い体験なので）。
    partner: int = 120
    opponent: int = 60
    # 優先度1: まだ組んでいない相手が残っているのに、同じ相手と2回目を組ませない。
    # 同じ相手とのペアは当人が必ず気づくので、対戦の重複よりずっと目立つ。
    # partner/opponent の増分だけでは「対戦相手を変えるためにペアを繰り返す」
    # 判断が起きる（ペアの重複1つを避けると、対戦の重複を2つ以上抱えることが多い）。
    # 2人とも、出場可能メンバーの中に未ペアの組んでよい相手が残っているときだけ効く。
    # 初心者の集中(700) より小さくする。これより大きいと、初心者を含む構成で
    # 初心者のペアを対面に集められなくなり、強さの差が 10 もある試合が出る
    # （16人2面・初心者2・ラケット3で、差6以上の試合が 500 なら 3件、1,200 以上で 9件）。
    # 初心者のいない構成では 500 で効果が出きる（16人4面の早すぎる重複 1.25→0/人）。
    premature_repeat: int = 500
    # 優先度1: 同じ4人で試合をさせない（ペアの分け方が違っても）。
    # 同じ顔ぶれで固まると、皆で集まってやっている感が薄れる。
    # premature_repeat だけだと、同じ4人を組み替えればペアは全部新しくなるので
    # （ab|cd → ac|bd）、そこに逃げて同じ4人が集中する（16人4面で顕著）。
    same_group: int = 240
    # 優先度1: 直前のラウンドと3人以上同じ顔ぶれの試合を作らない。
    # 同じ3人は長い目で見れば避けきれないので、続けて起きる場合だけを見る。
    recent_trio: int = 120
    # 優先度2: 参加回数の公平性（候補集合の slack 制限と併用する）
    fair: int = 100
    # 優先度3: 連続してマッチに入れない回数を最小にする
    sit_out: int = 120
    # 優先度3: ルールを覚えていない者同士のペアを避ける。3段階。
    # 初心者同士は実質ハード制約。ラケット経験者は1試合で慣れるので、より軽い。
    beginner_pair: int = 100_000
    beginner_racket_pair: int = 20_000
    racket_pair: int = 5_000
    # 優先度4: 初心者と組む回数を非初心者間で均等にする
    beginner_spread: int = 120
    # 優先度5: 初心者を含むペア同士でマッチを組む
    beginner_concentration: int = 700
    # 優先度5a: 対等なペア同士のマッチを増やす。左右のペアの強さの差に比例して減点。
    # 仕様ではばらけ（優先度1）の方が上なので、ばらけを潰さない程度に留める。
    # 実測では 0→15 で強さの差が 0.92→0.79、変異性の超過が +1.5pt。
    # 15→30 は差 0.14 の改善に +2.5pt かかり、効率が落ちる。
    strength_gap: int = 15
    # 優先度6: 男女ペア同士のマッチを優先する。
    # 仕様ではばらけ(優先度1)の方が上なので、ペア重複1回ぶん(240)より小さくする。
    gender: int = 60
    # 優先度6a: 男子ペア対女子ペアだけは別枠。実質的な禁止。
    gender_split: int = 20_000
    # 優先度7: 休み明けのメンバーをなるべく早くマッチに入れる
    just_returned: int = 40
    # スキップ（不採用）した編成を再び出さないための減点。
    # beginner_pair より小さくして、「初心者同士ペアを作ってでも別編成にする」を防ぐ。
    avoid_round: int = 50_000
    # 1試合ぶんの差し替えは、ルール未習得者同士ペア(5,000)より軽くする。
    # 同値だと「スキップ済みの試合を1つ避けるために仕様3c違反を作ってよい」
    # という順序になる。4面なら 4 倍されるので、3b(20,000)とも並んでしまう。
    avoid_match: int = 1_000


@dataclass(frozen=True)
class PlayerStat:
    """生成に必要な、あるメンバーの現在の状態と履歴。

    統計値は ``round_participation`` のスナップショットから導出されたもので、
    このデータクラス自体は DB を知らない。
    """

    id: int
    nickname: str
    gender: Gender
    level: Level
    baseline: int = 0
    """途中参加者の下駄。参加登録時点の active メンバーの最小 ``adjusted``。"""

    plays: int = 0
    """実際に出場した回数。"""

    rest_credit: int = 0
    """休憩によるみなし出場回数。連続休憩ブロック1つにつき ``長さ-1``。"""

    sit_out_streak: int = 0
    """末尾から続く「出場可能だったのに出番がなかった」回数。"""

    just_returned: bool = False
    """直前のラウンドを休憩していた（＝復帰した直後）。

    復帰後に1ラウンド出番がないと消える。「復帰してから一度も出ていない」
    ではないので、長く待たされている人は `sit_out_streak` の方で拾う。
    """

    status: MemberStatus = MemberStatus.ACTIVE

    @property
    def adjusted(self) -> int:
        """公平性の評価軸。この値が小さいほど優先的に出場させる。"""
        return self.baseline + self.plays + self.rest_credit

    @property
    def is_beginner(self) -> bool:
        """初心者かどうか。"""
        return self.level.is_beginner

    @property
    def knows_rules(self) -> bool:
        """ルールを覚えているか。"""
        return self.level.knows_rules

    @property
    def strength(self) -> int:
        """ペアの強さを見積もるための点数。レベルと性別で決まる。

        初心者は男女を区別しない。ボールが返せるかどうかの段階なので、
        パワーの差が意味を持たないため。
        """
        if self.level is Level.BEGINNER:
            return Level.BEGINNER.strength
        return self.level.strength + _GENDER_STRENGTH[self.gender]


def pair_key(a: int, b: int) -> tuple[int, int]:
    """メンバー2人の組を表す、順序によらないキー。"""
    return (a, b) if a <= b else (b, a)


@dataclass
class History:
    """これまでの採用ラウンドから導出した履歴。"""

    partner_count: dict[tuple[int, int], int] = field(default_factory=dict)
    """ペアを組んだ回数。キーは :func:`pair_key`。"""

    opponent_count: dict[tuple[int, int], int] = field(default_factory=dict)
    """対戦した回数。キーは :func:`pair_key`。"""

    beginner_partner_count: dict[int, int] = field(default_factory=dict)
    """非初心者が初心者とペアを組んだ回数。キーは非初心者の member_id。"""

    group_count: dict[tuple[int, int, int, int], int] = field(default_factory=dict)
    """同じ4人で試合をした回数（ペアの分け方は問わない）。キーは member_id の昇順。"""

    last_round_groups: tuple[tuple[int, int, int, int], ...] = ()
    """直前の採用ラウンドの、各試合の4人。member_id の昇順。"""

    def partners(self, a: int, b: int) -> int:
        """``a`` と ``b`` がペアを組んだ回数。"""
        return self.partner_count.get(pair_key(a, b), 0)

    def opponents(self, a: int, b: int) -> int:
        """``a`` と ``b`` が対戦した回数。"""
        return self.opponent_count.get(pair_key(a, b), 0)

    def beginner_partners(self, member_id: int) -> int:
        """``member_id`` が初心者とペアを組んだ回数。"""
        return self.beginner_partner_count.get(member_id, 0)


@dataclass(frozen=True)
class MatchPlan:
    """1コート分の試合。"""

    court_index: int
    team_a: tuple[int, int]
    team_b: tuple[int, int]

    @property
    def member_ids(self) -> tuple[int, int, int, int]:
        """出場する4名。"""
        return (*self.team_a, *self.team_b)

    def signature(self) -> tuple:
        """コート番号を無視した、編成の同一性を判定するための署名。"""
        return tuple(sorted([tuple(sorted(self.team_a)), tuple(sorted(self.team_b))]))


@dataclass(frozen=True)
class RoundPlan:
    """1回の生成結果（全コート分）。"""

    court_count: int
    """練習会が持つコート数。``matches`` より多ければ未使用コートがある。"""

    matches: tuple[MatchPlan, ...]
    playing: tuple[int, ...]
    sitting_out: tuple[int, ...]
    """出場可能だったが、このラウンドでは出番がなかったメンバー。"""

    resting: tuple[int, ...]
    score: int = 0

    @property
    def used_court_count(self) -> int:
        """実際に使うコート数。"""
        return len(self.matches)

    @property
    def unused_court_indexes(self) -> tuple[int, ...]:
        """使わないコートの番号。表示側で「使いません」と出すために使う。"""
        used = {m.court_index for m in self.matches}
        return tuple(i for i in range(self.court_count) if i not in used)

    def signature(self) -> tuple:
        """コートの入れ替えを無視した、ラウンド全体の編成の署名。"""
        return tuple(sorted(m.signature() for m in self.matches))
