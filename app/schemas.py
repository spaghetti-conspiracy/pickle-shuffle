"""API の入出力スキーマ。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import NAME_MAX, NICKNAME_MAX
from app.scheduler.domain import Gender, Level, MemberStatus, RoundStatus


class CourtOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    court_index: int
    name: str
    in_use: bool


#: 入力の長さの上限。列の長さ（app/models.py）と同じ値を使う。
#: SQLite は VARCHAR の長さを無視するので、ここで止めないとローカルでは
#: 通って PostgreSQL の本番だけ 500 になる。
COURT_NAME_MAX = NICKNAME_MAX


class CourtUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=COURT_NAME_MAX)
    in_use: bool | None = None


class SessionCreate(BaseModel):
    name: str = Field(max_length=NAME_MAX)
    court_count: int = Field(default=2, ge=1, le=4)


class SessionUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=NAME_MAX)
    highlight_beginners: bool | None = None
    timer_minutes: int | None = Field(default=None, ge=3, le=15)
    """1試合の持ち時間（分）。無制限にするには unlimited を使う。"""

    unlimited: bool = False
    """持ち時間を無制限にする。timer_minutes を None にしたいときの指定。"""


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    token: str
    """URL と API で使う識別子。連番の内部 id は外に出さない。"""

    name: str
    created_at: datetime
    highlight_beginners: bool
    """表示画面で初心者の名前を緑にするか。アルゴリズムの確認用。"""

    tennisbear_event_id: str | None
    """取り込み元のイベント ID。一度取り込んだら以後はここに固定する。

    保存は ``bear:1614380`` の形だが、画面に出すのは数字の部分だけ。
    取り込み元は ``import_source`` で別に返す。
    """

    import_source: str | None = None
    """取り込み元の名前（``bear``）。手で作った練習会は None。"""

    timer_minutes: int | None
    """1試合の持ち時間（分）。None なら無制限。"""

    courts: list[CourtOut]


class ImportRequest(BaseModel):
    """tennisbear のイベントID。URL の下から2番目の数字。"""

    event_id: int = Field(ge=1)


class ImportResultOut(BaseModel):
    added: list[str]
    """新しく登録したニックネーム。"""

    unchanged: int
    """すでに登録済みで、変更が無かった人数。"""

    resting: list[str]
    """一覧から居なくなったので休憩にした人。削除はしない。"""


class MemberCreate(BaseModel):
    """参加者を足す。台帳から選ぶか、その場で登録するかの2通り。

    ``person_id`` があればそれを使う。無ければ名前で新しく登録する
    （台帳にも入る）。打ち込んだ名前が台帳の誰かと同じでも、選んだのでは
    ないので別人として扱い、番号を振る。
    """

    person_id: int | None = None
    nickname: str = Field(default="", max_length=NICKNAME_MAX)
    gender: Gender = Gender.OTHER
    level: Level = Level.PICKLEBALL


class MemberUpdate(BaseModel):
    nickname: str | None = Field(default=None, max_length=NICKNAME_MAX)
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
    person_id: int | None = None
    """名簿の誰か。管理画面が「まだ参加していない人」を選ぶのに使う。"""


class PersonOut(BaseModel):
    """メンバー台帳の1人。"""

    id: int
    nickname: str
    gender: Gender
    level: Level
    source: str | None = None
    """どこから取り込んだ人か（``bear``）。手で登録した人は None。

    向こうの ID そのものは返さない。二重登録を見分けるための印。
    """

    source_label: str | None = None
    """画面に出す取り込み元の名前（``tennisbear``）。対応表はサーバに1つだけ置く。"""

    duplicate: bool = False
    """番号で見分けている同名がいるか（「マッツ」と「マッツ2」）。

    手で登録したあとに同じ人を取り込んでしまったときがこの形になる。
    統合はしないので、どちらを消すかを選ぶための手掛かりとして出す。
    """

    sessions: int = 0
    """いま参加者として入っている練習会の数。消す前の目安に使う。"""


class PersonCreate(BaseModel):
    nickname: str = Field(max_length=NICKNAME_MAX)
    gender: Gender = Gender.OTHER
    level: Level = Level.PICKLEBALL


class PersonUpdate(BaseModel):
    nickname: str | None = Field(default=None, max_length=NICKNAME_MAX)
    gender: Gender | None = None
    level: Level | None = None


class LoginRequest(BaseModel):
    """管理者の合言葉。いたずら防止であって、秘密を守る仕組みではない。"""

    password: str = Field(max_length=200)


class PlayerOut(BaseModel):
    id: int
    nickname: str
    gender: Gender
    level: Level
    unavailable: bool = False
    """表示中のマッチに入っているが、もう出られない人。

    生成のあとに休憩へ回ったか、外れた人。表示画面では暗くして、
    読み上げるときに気づけるようにする。マッチ自体は動かさない（不変則12）。
    """


class MatchOut(BaseModel):
    team_a: list[PlayerOut]
    team_b: list[PlayerOut]


class TimerOut(BaseModel):
    """試合時計の状態。

    **revision には含めない。** 経過秒は毎回変わるので、含めると表示画面が
    毎回描き直されてしまう。画面側はこの値だけ別に受け取って更新する。
    """

    state: str
    """stopped / running / paused。"""

    limit_seconds: int | None
    """持ち時間。None なら無制限。"""

    elapsed_seconds: int
    """動いていた秒数。画面はここを起点に自分で数える。"""

    timed_out: bool
    """持ち時間を過ぎたか。無制限のときは常に False。"""

    alarm_silenced: bool
    """時間切れの音を止めたか。どの端末で止めても全員に伝える。"""


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
    match_number: int | None = None
    """第n試合。練習会を通した1試合（1コート分）ごとの通し番号。試合が入っていなければ None。

    開始前（pending）の試合には、開始すれば付く番号を出す。
    """


class CurrentOut(BaseModel):
    server_instance: str
    """サーバのプロセスを識別する値。再起動すると変わる。

    表示画面はこれを覚えていて、変わったら知らせる。黙っていると、
    途切れた通信や消えた記録が「時計が狂った」ようにしか見えない。
    """

    session: SessionOut
    round_id: int | None
    round_status: RoundStatus | None
    revision: str
    """この応答の中身そのものの指紋。

    表示画面はこの値が変わったときだけ描き直す。中身から導くので、
    項目を増やしても入れ忘れが起きない。
    マッチの組み合わせはメンバーの編集では動かないため（不変則12）、
    これが変わって描き直しても試合中に組み合わせが変わることはない。
    """

    timer: TimerOut
    """試合時計。revision には入れない（経過秒が毎回変わるため）。"""

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
