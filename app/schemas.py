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
    highlight_beginners: bool | None = None


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    token: str
    """URL と API で使う識別子。連番の内部 id は外に出さない。"""

    name: str
    created_at: datetime
    highlight_beginners: bool
    """表示画面で初心者の名前を緑にするか。アルゴリズムの確認用。"""

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
    """コートの状態。

    * ``match`` — この回の試合が入っている
    * ``waiting`` — まだ生成していない。「次のマッチ」を押せば入る
    * ``next_round`` — 生成したあとに試合用へ戻したコート。次の生成から使われる
    * ``idle`` — 生成したが、出場できる人数が足りずこのコートは空き
    * ``practice`` — 練習用に試合から外している

    ``waiting`` と ``idle`` を混ぜると「13人いるのに人数が足りません」と出て、
    メンバー登録やコート設定を疑わせてしまうので分けてある。
    """

    match: MatchOut | None = None


class CurrentOut(BaseModel):
    session: SessionOut
    round_id: int | None
    round_status: RoundStatus | None
    revision: str
    """表示を描き直すべきときだけ変化する。

    ラウンド、コート、表示設定、そして食い違いの注意書き。
    マッチの中身を左右する値は入れないので、試合中に組み合わせが動くことはない。
    """

    courts: list[CourtStateOut]
    waiting: list[PlayerOut]
    resting: list[PlayerOut]
    stale_members: list[str]
    """いま表示中のマッチと食い違っているメンバー。

    生成後に休憩・離脱になった人と、生成後にレベルなどを変えた人。
    表示中のマッチは動かさないので、食い違いはここで伝える。
    """

    duplicate_nicknames: list[str]
    member_url: str = ""
    """メンバー用画面の URL。QR コードと同じもので、読み上げや共有に使う。"""


class StatsOut(BaseModel):
    adopted_rounds: int
    play_counts: dict[int, int]
