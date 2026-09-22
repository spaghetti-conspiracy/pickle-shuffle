"""API の入出力スキーマ。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.scheduler.domain import Gender, Level, MemberStatus, RoundStatus


class CourtOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    court_index: int
    name: str
    in_use: bool


class CourtUpdate(BaseModel):
    name: str | None = None
    in_use: bool | None = None


class SessionCreate(BaseModel):
    name: str
    court_count: int = Field(default=2, ge=1, le=4)


class SessionUpdate(BaseModel):
    name: str | None = None
    rotation: int | None = None


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    rotation: int
    created_at: datetime
    courts: list[CourtOut]


class MemberCreate(BaseModel):
    nickname: str
    gender: Gender = Gender.OTHER
    level: Level = Level.PICKLEBALL


class MemberUpdate(BaseModel):
    nickname: str | None = None
    gender: Gender | None = None
    level: Level | None = None
    status: MemberStatus | None = None


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nickname: str
    gender: Gender
    level: Level
    status: MemberStatus
    plays: int = 0


class MemberProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    nickname: str
    gender: Gender
    level: Level


class PlayerOut(BaseModel):
    id: int
    nickname: str
    gender: Gender
    level: Level


class MatchOut(BaseModel):
    team_a: list[PlayerOut]
    team_b: list[PlayerOut]


class CourtStateOut(BaseModel):
    """表示画面が1コート分を描くのに必要なもの。"""

    id: int
    court_index: int
    name: str
    state: str
    """``match`` = 試合あり / ``practice`` = 練習コート / ``idle`` = 人数が足りず未使用。"""

    match: MatchOut | None = None


class CurrentOut(BaseModel):
    session: SessionOut
    round_id: int | None
    round_status: RoundStatus | None
    revision: str
    """ラウンドとコートの状態が変わったときだけ変化する。メンバーの編集では変わらない。"""

    courts: list[CourtStateOut]
    waiting: list[PlayerOut]
    resting: list[PlayerOut]
    stale_members: list[str]
    """生成後に休憩や離脱になり、いま表示中のマッチと食い違っているメンバー。"""

    duplicate_nicknames: list[str]


class StatsOut(BaseModel):
    adopted_rounds: int
    play_counts: dict[int, int]
