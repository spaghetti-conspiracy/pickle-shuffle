"""ORM モデル。スキーマの正はこのファイル（CLAUDE.md 不変則4/8）。"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
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


def default_court_names(court_count: int = DEFAULT_COURT_COUNT) -> list[str]:
    """コート名の初期値。"""
    return [f"コート{i + 1}" for i in range(court_count)]


class PracticeSession(Base):
    """練習会。"""

    __tablename__ = "practice_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    court_count: Mapped[int] = mapped_column(Integer, default=DEFAULT_COURT_COUNT)
    court_names: Mapped[list[str]] = mapped_column(JSON, default=default_court_names)
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    """コートの配置。0=左右 / 90=上下 / 180=左右反転 / 270=上下反転。"""

    random_seed: Mapped[int] = mapped_column(BigInteger, default=new_random_seed)
    """この練習会の非決定性の種。作成時に採番し、以後不変。"""

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
    baseline: Mapped[int] = mapped_column(Integer, default=0)
    """途中参加者の下駄。登録時点の active メンバーの最小 adjusted。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[PracticeSession] = relationship(back_populates="members")


class Round(Base):
    """1回の生成単位（全コート分）。"""

    __tablename__ = "rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("practice_sessions.id", ondelete="CASCADE"), index=True
    )
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
        order_by="Match.court_index",
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
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), index=True)
    court_index: Mapped[int] = mapped_column(Integer)

    round: Mapped[Round] = relationship(back_populates="matches")
    slots: Mapped[list[MatchSlot]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="MatchSlot.id",
    )


class MatchSlot(Base):
    """試合の出場枠。1試合あたり4行（チーム0が2行、チーム1が2行）。"""

    __tablename__ = "match_slots"

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

    round: Mapped[Round] = relationship(back_populates="participations")


class MemberProfile(Base):
    """ニックネームをキーにした属性の辞書。練習会には属さない。

    保持するのは属性だけで、統計は絶対に共有しない（不変則13）。
    重複時は last-write-wins で振動してよい。
    """

    __tablename__ = "member_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    nickname: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    gender: Mapped[Gender] = mapped_column(_enum_column(Gender))
    level: Mapped[Level] = mapped_column(_enum_column(Level))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
