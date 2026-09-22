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
    """レベル区分。

    仕様上「ルールがわからないラケットスポーツ経験者」は経験者と区別しないため、
    生成ロジックでは :attr:`is_beginner` だけを見る。
    """

    BEGINNER = "beginner"
    """ルールがわからないラケットスポーツ未経験者。"""

    RACKET_EXPERIENCED = "racket_experienced"
    """ルールがわからないラケットスポーツ経験者。生成上は経験者扱い。"""

    PICKLEBALL = "pickleball"
    """ピックルボール経験者。"""

    @property
    def is_beginner(self) -> bool:
        """初心者（ラケットスポーツ未経験者）かどうか。"""
        return self is Level.BEGINNER


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

    重みの大小関係は仕様の優先順位を反映している（1試合ぶんの差分で比較する）。
        初心者ペアの集中 700 > 連続不参加 360 > ペア重複 240 > 参加回数 200
        > 初心者の相手の偏り 240 ≒ 対戦重複 120 > 男女組合せ 60/120
    初心者同士ペアと男子ペア対女子ペアだけは桁を変えて実質ハード制約にしてある。

    値は実測で決めた。判断の基準にしたのは、ペアと対戦の重複回数の二乗和が
    「同じ総数を均等に配分したときの理論最小値」からどれだけ超過するかで、
    重みに依存しないため異なる配分どうしを公平に比べられる。
    この配分での実測（3時間ぶん24〜25ラウンド、5〜16名の12構成）:
        参加回数差 0〜1 / 連続不参加 2以下 / 初心者同士ペア 0件 /
        初心者の同一コート集中 96% / 男子ペア対女子ペア 0件 /
        ペアと対戦の重複の理論超過 約12%
    """

    # 優先度1: ペア・対戦の組合せがばらけること。
    # 1ラウンドで作られるペアは4組、対戦は8組なので、この比なら両者の影響が釣り合う。
    # 1件あたりの重みはペアの方が2倍（組む相手の方が強い体験なので）。
    partner: int = 120
    opponent: int = 60
    # 優先度2: 参加回数の公平性（候補集合の slack 制限と併用する）
    fair: int = 100
    # 優先度3: 連続してマッチに入れない回数を最小にする
    sit_out: int = 120
    # 優先度3: 初心者同士のペアを避ける（実質ハード制約）
    beginner_pair: int = 100_000
    # 優先度4: 初心者と組む回数を非初心者間で均等にする
    beginner_spread: int = 120
    # 優先度5: 初心者を含むペア同士でマッチを組む
    beginner_concentration: int = 700
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
    avoid_match: int = 5_000


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
    """休憩から復帰した直後で、まだ1度も出場していない。"""

    status: MemberStatus = MemberStatus.ACTIVE

    @property
    def adjusted(self) -> int:
        """公平性の評価軸。この値が小さいほど優先的に出場させる。"""
        return self.baseline + self.plays + self.rest_credit

    @property
    def is_beginner(self) -> bool:
        """初心者かどうか。"""
        return self.level.is_beginner


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
