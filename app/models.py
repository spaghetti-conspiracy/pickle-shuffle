"""ORM モデル。スキーマの正はこのファイル（CLAUDE.md 不変則4/8）。"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.scheduler.domain import (
    Gender,
    Level,
    MemberStatus,
    ParticipationState,
    RoundStatus,
)

DEFAULT_COURT_COUNT = 2


def _enum_column(enum_cls: type) -> SAEnum:
    """Enum を可搬な VARCHAR + CHECK として持つ。

    ネイティブ ENUM 型は DB ごとに扱いが違うため使わない（CLAUDE.md 不変則4）。
    値はメンバー名ではなく ``value`` を保存する。
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=30,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def utcnow() -> datetime:
    """タイムゾーン付きの現在時刻。"""
    return datetime.now(timezone.utc)


def new_random_seed() -> int:
    """練習会の乱数シードを採番する。以後この値は変えない（不変則11）。"""
    return secrets.randbits(63)


#: トークンに使う文字。読み上げや手入力で取り違えないよう
#: 0/O、1/l/I は入れない。31種類。
TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
TOKEN_LENGTH = 10


def new_session_token() -> str:
    """URL に出す練習会の識別子。

    連番だと、メンバーが自分の練習会の URL から他の練習会を推測できてしまう。
    認証は付けない方針なので、推測できない値にすることで守る。
    31種から10文字なので総数は 31^10 ≈ 8.2 x 10^14。総当たりでは当たらない。
    """
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def default_court_name(court_index: int) -> str:
    """コート名の初期値。"""
    return f"コート{court_index + 1}"


class PracticeSession(Base):
    """練習会。"""

    __tablename__ = "practice_sessions"
    __table_args__ = (UniqueConstraint("name", name="uq_session_name"),)
    """名前は重複させない。

    選択画面はプルダウンに名前だけを出すので、同名があると見分けられない。
    終了した練習会は削除されるため、次の週には同じ名前を使える。
    """

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(
        String(TOKEN_LENGTH), unique=True, index=True, default=new_session_token
    )
    """URL と API で使う識別子。連番の id は外に出さない。"""

    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    highlight_beginners: Mapped[bool] = mapped_column(Boolean, default=False)
    """表示画面で初心者の名前を緑にするか。アルゴリズムの確認用で、ふだんは off。"""

    random_seed: Mapped[int] = mapped_column(BigInteger, default=new_random_seed)
    """この練習会の非決定性の種。作成時に採番し、以後不変。"""

    courts: Mapped[list[Court]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Court.court_index",
    )
    members: Mapped[list[Member]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Member.id",
    )
    rounds: Mapped[list[Round]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Round.id",
    )


class Court(Base):
    """コート。練習会の作成時に最大数ぶん作り、以後は増減させない。

    途中で ``in_use`` を落とすと試合には使われなくなる。初心者の育成用に
    練習コートとして空けておく、といった使い方を想定している。
    """

    __tablename__ = "courts"
    __table_args__ = (UniqueConstraint("session_id", "court_index", name="uq_court_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("practice_sessions.id", ondelete="CASCADE"), index=True
    )
    court_index: Mapped[int] = mapped_column(Integer)
    """0 起点の並び順。表示の並びもこの順。"""

    name: Mapped[str] = mapped_column(String(50))
    in_use: Mapped[bool] = mapped_column(Boolean, default=True)
    """試合に使うか。False なら練習コートとして試合から外す。"""

    session: Mapped[PracticeSession] = relationship(back_populates="courts")


class Member(Base):
    """練習会に参加するメンバー。

    統計はこのテーブルには持たない。参加回数などは
    :class:`RoundParticipation` のスナップショットから毎回導出する（不変則2）。
    """

    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("practice_sessions.id", ondelete="CASCADE"), index=True
    )
    nickname: Mapped[str] = mapped_column(String(50))
    gender: Mapped[Gender] = mapped_column(_enum_column(Gender))
    level: Mapped[Level] = mapped_column(_enum_column(Level))
    status: Mapped[MemberStatus] = mapped_column(
        _enum_column(MemberStatus), default=MemberStatus.ACTIVE
    )
    tennisbear_user_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    """取り込み元の tennisbear のユーザ ID。手で登録した人は None。

    再取り込みのときに「もう登録済みか」を照合するために持つ。
    ニックネームは識別子ではない（不変則14）ので、名前では照合できない。
    画面には出さない。
    """

    baseline: Mapped[int] = mapped_column(Integer, default=0)
    """途中参加者の下駄。登録時点の active メンバーの最小 adjusted。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    """最後に属性や状態を変えた時刻。

    生成済みのマッチより後に変わったかどうかを見て、表示画面で注意を出すのに使う。
    """

    session: Mapped[PracticeSession] = relationship(back_populates="members")


class Round(Base):
    """1回の生成単位（全コート分）。"""

    __tablename__ = "rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("practice_sessions.id", ondelete="CASCADE"), index=True
    )
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_round_seq"),)
    """採用の通し番号は練習会の中で一意にする。

    同じ seq が2本できると、不変則11の `(seed, round_seq, attempt)` から
    同じ編成が導かれ、統計の順序付けも壊れる。pending / rejected は seq が
    NULL で、NULL は SQLite でも PostgreSQL でも重複を許されるため妨げない。
    """

    seq: Mapped[int | None] = mapped_column(Integer, default=None)
    """採用時にのみ採番する 1 起点の通し番号。pending / rejected では None。"""

    status: Mapped[RoundStatus] = mapped_column(
        _enum_column(RoundStatus), default=RoundStatus.PENDING
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    """同じ位置で何回生成し直したか。乱数の導出と再生成の差別化に使う。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    session: Mapped[PracticeSession] = relationship(back_populates="rounds")
    matches: Mapped[list[Match]] = relationship(
        back_populates="round",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    participations: Mapped[list[RoundParticipation]] = relationship(
        back_populates="round",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Match(Base):
    """1コート分の試合。"""

    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    __table_args__ = (UniqueConstraint("round_id", "court_id", name="uq_match_court"),)
    """1ラウンドの同じコートに2試合は入らない。

    入ると表示側が後勝ちで上書きし、片方が黙って消える。
    読み上げに使う画面なので、消えるより落ちた方がよい。
    """

    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), index=True)
    court_id: Mapped[int] = mapped_column(ForeignKey("courts.id", ondelete="CASCADE"), index=True)

    round: Mapped[Round] = relationship(back_populates="matches")
    court: Mapped[Court] = relationship()
    slots: Mapped[list[MatchSlot]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="MatchSlot.id",
    )


class MatchSlot(Base):
    """試合の出場枠。1試合あたり4行（チーム0が2行、チーム1が2行）。"""

    __tablename__ = "match_slots"

    __table_args__ = (UniqueConstraint("match_id", "member_id", name="uq_slot_member"),)
    """同じ人が同じ試合に2枠入らない。"""

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    team_index: Mapped[int] = mapped_column(Integer)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)

    match: Mapped[Match] = relationship(back_populates="slots")
    member: Mapped[Member] = relationship()


class RoundParticipation(Base):
    """採用ラウンド1回分の、全メンバーの状態スナップショット。

    統計はすべてここから導出する。不採用ラウンドには作らない（不変則2/3）。
    """

    __tablename__ = "round_participation"
    __table_args__ = (UniqueConstraint("round_id", "member_id", name="uq_participation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    state: Mapped[ParticipationState] = mapped_column(_enum_column(ParticipationState))
    level: Mapped[Level] = mapped_column(_enum_column(Level))
    """そのラウンド時点のレベル。

    途中でレベルを変えても過去の履歴が書き換わらないように記録する。
    現在のレベルで過去を解釈すると、「初心者と組んだ回数」が消えたり
    遡って計上されたりして、負担の均しが狂う。
    """

    round: Mapped[Round] = relationship(back_populates="participations")


class MemberProfile(Base):
    """属性の辞書。練習会には属さない。

    保持するのは属性だけで、統計は絶対に共有しない（不変則13）。
    引き当ては tennisbear の ID があればそちらを優先し、無ければニックネーム。
    ニックネームは識別子ではない（不変則14）ので、重複時は last-write-wins で
    振動してよい、という運用方針は変えない。
    """

    __tablename__ = "member_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    nickname: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    tennisbear_user_id: Mapped[int | None] = mapped_column(
        Integer, unique=True, index=True, default=None
    )
    """取り込み元のユーザ ID。手で登録した人は None。

    こちらで引き当てられると、改名しても属性を見失わない。
    管理者が直したレベルが次の練習会でも使われる。
    """

    gender: Mapped[Gender] = mapped_column(_enum_column(Gender))
    level: Mapped[Level] = mapped_column(_enum_column(Level))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
