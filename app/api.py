"""HTTP API。ドメインの例外は main.py で HTTP ステータスへ変換する。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from io import BytesIO
from typing import Annotated

import segno
from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy.orm import Session

from app import __version__, auth, tennisbear
from app.config import IS_SERVERLESS, settings
from app.db import get_db
from app.errors import ConflictError, UnauthorizedError, ValidationError
from app.external import (
    SOURCE_LABELS,
    SOURCE_TENNISBEAR,
    external_key,
    raw_id_of,
    source_of,
)
from app.models import Member, Owner, Person, PracticeSession, Round, TimerState
from app.scheduler.domain import MemberStatus, RoundStatus
from app.scheduler.generator import PLAYERS_PER_MATCH
from app.schemas import (
    CourtOut,
    CourtStateOut,
    CourtUpdate,
    CurrentOut,
    ImportRequest,
    ImportResultOut,
    LoginRequest,
    MatchOut,
    MemberCreate,
    MemberOut,
    MemberUpdate,
    PersonCreate,
    PersonOut,
    PersonUpdate,
    PlayerOut,
    SessionCreate,
    SessionOut,
    SessionUpdate,
    StatsOut,
    TimerOut,
)
from app.services import owners as owners_service
from app.services import people as people_service
from app.services import rounds as rounds_service
from app.services import sessions as sessions_service
from app.services import stats as stats_service

#: このプロセスが起動したときに決める値。再起動すると変わる。
#:
#: 表示画面はこれを覚えていて、変わったら「サーバが再起動しました」と伝える。
#: 再起動をまたぐと、応答が一時的に途切れたり、作り直した直後なら記録が
#: 消えていたりする。黙っていると「時計が狂った」ようにしか見えない。
#: サーバーレスでは関数インスタンスが複数同時に生きるので、プロセスを
#: 識別しても意味が無い。空にして、画面側の知らせを出さないようにする。
SERVER_INSTANCE = "" if IS_SERVERLESS else secrets.token_hex(8)

DbSession = Annotated[Session, Depends(get_db)]
"""リクエストごとの DB セッション。"""


# ---------------------------------------------------------------------------
# 合言葉
#
# **いたずら防止であって、秘密を守る仕組みではない。** 知らない人に練習会を
# 作られたり名簿を覗かれたりしないようにするだけ。
#
# メンバー用画面は QR を読んだ人がその場で開くので、ここに合言葉を掛ける
# わけにいかない。逆に、管理画面と全体表示画面はトップ画面を通ってから開く
# ものとして、合言葉を求める。
# ---------------------------------------------------------------------------


def require_admin(
    db: DbSession,
    pickle_admin: Annotated[str | None, Cookie(alias=auth.COOKIE_NAME)] = None,
) -> Owner:
    """合言葉を通しているか見て、いま見ている団体を返す。

    ルータ全体の依存としても、団体が要る endpoint の依存としても使う。
    FastAPI は同じリクエストの中で1度しか解決しないので、二重には効かない。
    """
    admin_id = auth.read_cookie(pickle_admin)
    admin = owners_service.get_admin(db, admin_id) if admin_id is not None else None
    if admin is None or not auth.cookie_matches(
        pickle_admin or "", admin.id, admin.password_hash
    ):
        raise UnauthorizedError("合言葉を入力してください")
    return owners_service.current_owner(db, admin)


CurrentOwner = Annotated[Owner, Depends(require_admin)]
"""合言葉を通した管理者が見ている団体。"""


def _gate(owner: CurrentOwner) -> None:
    """ルータ全体に掛ける門。団体は各 endpoint が自分で受け取る。"""


public_router = APIRouter(prefix="/api")
"""合言葉の要らない入口。

メンバー用画面が使う read と、合言葉そのものだけ。
"""

router = APIRouter(prefix="/api", dependencies=[Depends(_gate)])
"""合言葉が要る入口。

ルータを分けたのは、**新しい API を足したときに既定で守られる**ようにするため。
公開してよいものだけ `public_router` に置く。
"""


@public_router.get("/health")
def health() -> dict[str, str]:
    """死活監視用。"""
    return {"status": "ok"}


@router.get("/version")
def version() -> dict[str, str]:
    """アプリのバージョン。選択画面の一番下に出す。

    合言葉の要る側に置く（公開する入口は増やさない）。health の応答は CI と
    リリースの点検が完全一致で照合しているので、そこには足さない。
    """
    return {"version": __version__}


@public_router.post("/login", status_code=204)
def login(payload: LoginRequest, response: Response, db: DbSession) -> None:
    """合言葉を確かめ、通ったことをクッキーに残す。"""
    admin = owners_service.authenticate(db, payload.password)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_cookie(admin.id, admin.password_hash),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )


@public_router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie(auth.COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# 練習会
# ---------------------------------------------------------------------------


def _session_out(session: PracticeSession) -> SessionOut:
    return SessionOut(
        token=session.token,
        name=session.name,
        created_at=session.created_at,
        highlight_beginners=session.highlight_beginners,
        tennisbear_event_id=raw_id_of(session.external_event_id),
        import_source=source_of(session.external_event_id),
        timer_minutes=session.timer_minutes,
        courts=[CourtOut.model_validate(c) for c in session.courts],
    )


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(db: DbSession, owner: CurrentOwner) -> list[SessionOut]:
    return [_session_out(s) for s in sessions_service.list_sessions(db, owner)]


@router.post("/sessions", response_model=SessionOut, status_code=201)
def create_session(
    payload: SessionCreate, db: DbSession, owner: CurrentOwner
) -> SessionOut:
    session = sessions_service.create_session(
        db, owner, payload.name, payload.court_count
    )
    return _session_out(session)


@router.get("/sessions/{session_token}", response_model=SessionOut)
def get_session(session_token: str, db: DbSession) -> SessionOut:
    return _session_out(sessions_service.get_session(db, session_token))


@router.patch("/sessions/{session_token}", response_model=SessionOut)
def update_session(
    session_token: str, payload: SessionUpdate, db: DbSession
) -> SessionOut:
    session = sessions_service.get_session(db, session_token)
    sessions_service.update_session(
        db,
        session,
        name=payload.name,
        highlight_beginners=payload.highlight_beginners,
        timer_minutes=payload.timer_minutes,
        unlimited=payload.unlimited,
    )
    return _session_out(session)


@router.delete("/sessions/{session_token}", status_code=204)
def delete_session(session_token: str, db: DbSession) -> None:
    sessions_service.delete_session(db, sessions_service.get_session(db, session_token))


@router.patch("/sessions/{session_token}/courts/{court_id}", response_model=CourtOut)
def update_court(
    session_token: str,
    court_id: int,
    payload: CourtUpdate,
    db: DbSession,
) -> CourtOut:
    """コート名の変更と、試合用から外す/戻す。"""
    session = sessions_service.get_session(db, session_token)
    court = sessions_service.update_court(
        db, session, court_id, name=payload.name, in_use=payload.in_use
    )
    return CourtOut.model_validate(court)


# ---------------------------------------------------------------------------
# メンバー
# ---------------------------------------------------------------------------


def _member_out(member: Member, plays: dict[int, int]) -> MemberOut:
    return MemberOut(
        id=member.id,
        nickname=member.nickname,
        gender=member.gender,
        level=member.level,
        status=member.status,
        plays=plays.get(member.id, 0),
        person_id=member.person_id,
    )


@router.get("/sessions/{session_token}/members", response_model=list[MemberOut])
def list_members(session_token: str, db: DbSession) -> list[MemberOut]:
    session = sessions_service.get_session(db, session_token)
    plays = stats_service.play_counts(db, session.id)
    return [
        _member_out(m, plays) for m in sessions_service.list_members(db, session.id)
    ]


@router.post("/sessions/{session_token}/members", response_model=MemberOut, status_code=201)
def add_member(
    session_token: str, payload: MemberCreate, db: DbSession
) -> MemberOut:
    """参加者を足す。台帳から選ぶか、その場で登録するか。

    その場で登録した人は台帳にも入る。打ち込んだ名前が台帳の誰かと同じでも、
    選んだのではないので別人として扱う（同名は番号で見分ける・不変則14）。
    """
    session = sessions_service.get_session(db, session_token)
    owner = sessions_service.owner_of(db, session)
    if payload.person_id is not None:
        person = people_service.get_person(db, owner, payload.person_id)
    else:
        person = people_service.add_person(
            db,
            owner,
            nickname=payload.nickname,
            gender=payload.gender,
            level=payload.level,
        )
    member = sessions_service.add_member(db, session, person)
    return _member_out(member, {})


@router.post(
    "/sessions/{session_token}/members/import", response_model=ImportResultOut
)
def import_members(
    session_token: str, payload: ImportRequest, db: DbSession
) -> ImportResultOut:
    """tennisbear のイベントから参加者を取り込む。

    何度でも実行してよい。すでに台帳にいる人は ID で見分けて、**何も書き換えない**。
    向こうと繋がっているのはユーザ ID だけで、名前や属性はこちらで管理する。
    """
    session = sessions_service.get_session(db, session_token)
    owner = sessions_service.owner_of(db, session)
    # 取りに行く前に弾く。別のイベントだと分かっているのに外へ出ても無駄で、
    # そのIDが実在しなければ「見つかりません」が先に返って理由がぼやける。
    sessions_service.check_event(
        session, external_key(SOURCE_TENNISBEAR, payload.event_id)
    )
    html = tennisbear.fetch_event_page(
        payload.event_id,
        base_url=settings.tennisbear_base_url,
        timeout=settings.tennisbear_timeout,
    )
    participants = tennisbear.parse_event_page(html)
    result = sessions_service.import_participants(
        db, owner, session, participants, event_id=payload.event_id
    )
    return ImportResultOut(
        added=result.added,
        unchanged=result.unchanged,
        resting=result.resting,
    )


@router.post("/rounds/{round_id}/timer/{action}", response_model=CurrentOut)
def control_timer(
    round_id: int,
    action: str,
    request: Request,
    db: DbSession,
    owner: CurrentOwner,
) -> CurrentOut:
    """試合時計を操作する。一時停止・再開・中断。

    開始は採用（`/adopt`）と同時なので、ここには無い。
    """
    round_ = rounds_service.get_round(db, round_id, owner.id)
    if round_.status is not RoundStatus.ADOPTED:
        # 開始前のラウンドを消音すると、始めた瞬間から鳴らない試合になる。
        raise ConflictError("始まっていないマッチの時計は操作できません")
    handlers = {
        "pause": rounds_service.pause_timer,
        "resume": rounds_service.resume_timer,
        "stop": rounds_service.stop_timer,
        "silence": rounds_service.silence_alarm,
    }
    handler = handlers.get(action)
    if handler is None:
        raise ValidationError("その操作はできません")
    handler(db, round_)
    session = sessions_service.get_session_by_id(db, round_.session_id)
    return _build_current(db, session, request)


@router.patch("/members/{member_id}", response_model=MemberOut)
def update_member(
    member_id: int, payload: MemberUpdate, db: DbSession, owner: CurrentOwner
) -> MemberOut:
    member = sessions_service.get_member(db, member_id, owner)
    sessions_service.update_member(
        db,
        member,
        nickname=payload.nickname,
        gender=payload.gender,
        level=payload.level,
        status=payload.status,
    )
    return _member_out(member, stats_service.play_counts(db, member.session_id))


@router.delete("/members/{member_id}", status_code=204)
def remove_member(member_id: int, db: DbSession, owner: CurrentOwner) -> None:
    """この練習会の参加者リストから外す。**台帳からは消えない。**"""
    sessions_service.remove_member(db, sessions_service.get_member(db, member_id, owner))


# ---------------------------------------------------------------------------
# メンバー台帳
#
# 練習会には属さない「人」の一覧。ここでの削除は本当に消すが、練習会の
# 参加者と過去の記録には触らない（進行中の練習会が壊れないように）。
# **取り込みの導線はここには置かない。** 実際のイベントに紐づくときしか
# 外部から引かない、という歯止めのため。
# ---------------------------------------------------------------------------


def _person_out(person: Person, *, duplicate: bool, sessions: int) -> PersonOut:
    return PersonOut(
        id=person.id,
        nickname=person.nickname,
        gender=person.gender,
        level=person.level,
        source=people_service.source_label(person),
        source_label=SOURCE_LABELS.get(people_service.source_label(person) or ""),
        duplicate=duplicate,
        sessions=sessions,
    )


def _people_out(db: Session, owner: Owner) -> list[PersonOut]:
    people = people_service.list_people(db, owner)
    numbered = people_service.numbered_pairs(people)
    joined = people_service.session_counts(db, owner)
    return [
        _person_out(
            person,
            duplicate=person.id in numbered,
            sessions=joined.get(person.id, 0),
        )
        for person in people
    ]


@router.get("/people", response_model=list[PersonOut])
def list_people(db: DbSession, owner: CurrentOwner) -> list[PersonOut]:
    return _people_out(db, owner)


@router.post("/people", response_model=PersonOut, status_code=201)
def add_person(
    payload: PersonCreate, db: DbSession, owner: CurrentOwner
) -> PersonOut:
    person = people_service.add_person(
        db,
        owner,
        nickname=payload.nickname,
        gender=payload.gender,
        level=payload.level,
    )
    return _person_out(person, duplicate=False, sessions=0)


@router.patch("/people/{person_id}", response_model=PersonOut)
def update_person(
    person_id: int, payload: PersonUpdate, db: DbSession, owner: CurrentOwner
) -> PersonOut:
    """台帳を直す。その人が入っている練習会の参加者にも反映される。"""
    person = people_service.get_person(db, owner, person_id)
    people_service.update_person(
        db,
        owner,
        person,
        nickname=payload.nickname,
        gender=payload.gender,
        level=payload.level,
    )
    joined = people_service.session_counts(db, owner)
    return _person_out(person, duplicate=False, sessions=joined.get(person.id, 0))


@router.delete("/people/{person_id}", status_code=204)
def delete_person(person_id: int, db: DbSession, owner: CurrentOwner) -> None:
    """台帳から消す。練習会の参加者と過去の記録はそのまま残る。"""
    people_service.delete_person(db, owner, people_service.get_person(db, owner, person_id))


# ---------------------------------------------------------------------------
# ラウンド
# ---------------------------------------------------------------------------


def _player_out(member: Member, *, unavailable: bool = False) -> PlayerOut:
    return PlayerOut(
        id=member.id,
        nickname=member.nickname,
        gender=member.gender,
        level=member.level,
        unavailable=unavailable,
    )


def _as_utc(value: datetime) -> datetime:
    """naive な日時を UTC とみなして揃える。

    保存はどちらも UTC だが、SQLite は tz を落として返すため、
    そのまま比べると PostgreSQL 側の aware な値と比較できない。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _stale_member_ids(
    members: dict[int, Member], playing: set[int], round_: Round | None
) -> list[int]:
    """表示中のマッチと食い違っているメンバー。

    休憩・離脱に加えて、生成より後に属性（レベルなど）を変えた人も含める。
    表示中のマッチ自体は動かさない方針（不変則12）なので、
    食い違いは画面の注意書きで伝えるしかない。
    """
    if round_ is None:
        return []
    generated_at = _as_utc(round_.created_at)
    stale = []
    for member_id in playing:
        member = members.get(member_id)
        if member is None:
            continue
        changed_after = _as_utc(member.updated_at) > generated_at
        if member.status is not MemberStatus.ACTIVE or changed_after:
            stale.append(member_id)
    return sorted(stale, key=lambda i: members[i].nickname)


def _timer_out(session: PracticeSession, round_: Round | None) -> TimerOut:
    """試合時計の現在値。

    残り時間ではなく経過時間を返す。画面側はここを起点に自分で数えるので、
    ポーリングの間隔より細かく動かせる。持ち時間の設定を試合中に変えても、
    経過はそのままなので残り時間だけが変わる。
    """
    limit = session.timer_minutes * 60 if session.timer_minutes else None
    if round_ is None or round_.status is not RoundStatus.ADOPTED:
        return TimerOut(
            state=TimerState.STOPPED.value,
            limit_seconds=limit,
            elapsed_seconds=0,
            timed_out=False,
            alarm_silenced=False,
        )
    elapsed = rounds_service.elapsed_seconds(round_)
    timed_out = bool(limit) and elapsed >= limit
    return TimerOut(
        state=round_.timer_state.value,
        limit_seconds=limit,
        elapsed_seconds=elapsed,
        timed_out=timed_out,
        # 持ち時間を延ばして時間内に戻ったら、消音も解く。残したままだと、
        # 延長後に本当に時間切れになったとき、全端末で無音になる。
        alarm_silenced=round_.timer_alarm_silenced and timed_out,
    )


def _revision_of(payload: CurrentOut) -> str:
    """表示すべき内容そのものから導く指紋。

    表示画面はこの値が変わったときだけ描き直す。値を手で組み立てると
    「コート名を入れ忘れて、変えても反映されない」といった取りこぼしが
    必ず起きるので、応答の中身をまるごと材料にする。

    不変則12（表示中のマッチを動かさない）は、ここではなく生成側で守る。
    メンバーを編集しても `MatchSlot` は変わらないので、組み合わせは動かない。
    動くのは名前・色・注意書きといった表示だけで、それは動いてほしい。
    """
    # timer は毎回変わるので指紋に入れない。入れると2秒ごとに描き直しになる。
    # timer は毎回変わるので指紋に入れない。入れると2秒ごとに描き直しになる。
    # server_instance も同じ。サーバーレスでは関数インスタンスごとに違う値になり、
    # リクエストのたびに指紋が変わってしまう。
    body = payload.model_dump_json(exclude={"revision", "timer", "server_instance"})
    return hashlib.blake2b(body.encode("utf-8"), digest_size=8).hexdigest()


def _build_current(
    db: Session, session: PracticeSession, request: Request | None = None
) -> CurrentOut:
    members = {m.id: m for m in sessions_service.list_members(db, session.id)}
    round_ = rounds_service.current_round(db, session.id)

    match_by_court: dict[int, MatchOut] = {}
    playing: set[int] = set()
    if round_ is not None:
        for match in round_.matches:
            teams: dict[int, list[PlayerOut]] = {0: [], 1: []}
            for slot in match.slots:
                member = members.get(slot.member_id)
                if member is None:
                    continue
                teams[slot.team_index].append(
                    _player_out(
                        member, unavailable=member.status is not MemberStatus.ACTIVE
                    )
                )
                playing.add(member.id)
            match_by_court[match.court_id] = MatchOut(team_a=teams[0], team_b=teams[1])

    waiting_count = sum(
        1
        for m in members.values()
        if m.status is MemberStatus.ACTIVE and m.id not in playing
    )

    court_states = []
    for court in session.courts:
        if court.id in match_by_court:
            state = "match"
        elif not court.in_use:
            state = "practice"
        elif round_ is None:
            # まだ1度も生成していない（またはスキップ直後）。人数の問題ではないので、
            # 「人数が足りません」と出すと設定を疑わせてしまう。
            state = "waiting"
        elif waiting_count >= PLAYERS_PER_MATCH:
            # 埋められるだけの人が待っているのに試合が入っていない。
            # 生成したあとにこのコートを試合用へ戻した、という状況。
            # ここで「人数が足りません」と出すと、戻した操作が効いていないように見える。
            state = "next_round"
        else:
            state = "idle"
        court_states.append(
            CourtStateOut(
                id=court.id,
                court_index=court.court_index,
                name=court.name,
                state=state,
                match=match_by_court.get(court.id),
            )
        )

    waiting = [
        _player_out(m)
        for m in members.values()
        if m.status is MemberStatus.ACTIVE and m.id not in playing
    ]
    resting = [
        _player_out(m) for m in members.values() if m.status is MemberStatus.RESTING
    ]
    stale_ids = _stale_member_ids(members, playing, round_)
    stale = [members[i].nickname for i in stale_ids]

    payload = CurrentOut(
        server_instance=SERVER_INSTANCE,
        session=_session_out(session),
        round_id=round_.id if round_ else None,
        round_status=round_.status if round_ else None,
        revision="",
        timer=_timer_out(session, round_),
        courts=court_states,
        waiting=waiting,
        resting=resting,
        stale_members=stale,
        duplicate_nicknames=rounds_duplicate_names(db, round_),
        member_url=member_page_url(request, session.token) if request else "",
    )
    payload.revision = _revision_of(payload)
    return payload


def rounds_duplicate_names(db: Session, round_: Round | None) -> list[str]:
    return sessions_service.match_duplicate_nicknames(db, round_.id if round_ else None)


@public_router.get("/sessions/{session_token}/current", response_model=CurrentOut)
def get_current(session_token: str, request: Request, db: DbSession) -> CurrentOut:
    """表示画面が2秒おきに読むエンドポイント。"""
    return _build_current(db, sessions_service.get_session(db, session_token), request)


@router.post("/sessions/{session_token}/rounds/generate", response_model=CurrentOut)
def generate_round_api(session_token: str, request: Request, db: DbSession) -> CurrentOut:
    session = sessions_service.get_session(db, session_token)
    rounds_service.generate(db, session)
    db.refresh(session)
    return _build_current(db, session, request)


@router.post("/rounds/{round_id}/adopt", response_model=CurrentOut)
def adopt_round(round_id: int, request: Request, db: DbSession, owner: CurrentOwner) -> CurrentOut:
    round_ = rounds_service.get_round(db, round_id, owner.id)
    rounds_service.adopt(db, round_)
    session = sessions_service.get_session_by_id(db, round_.session_id)
    return _build_current(db, session, request)


@router.post("/rounds/{round_id}/reject", response_model=CurrentOut)
def reject_round(round_id: int, request: Request, db: DbSession, owner: CurrentOwner) -> CurrentOut:
    round_ = rounds_service.get_round(db, round_id, owner.id)
    session_id = round_.session_id
    rounds_service.reject(db, round_)
    session = sessions_service.get_session_by_id(db, session_id)
    return _build_current(db, session, request)


@router.post("/rounds/{round_id}/undo", response_model=CurrentOut)
def undo_round(round_id: int, request: Request, db: DbSession, owner: CurrentOwner) -> CurrentOut:
    round_ = rounds_service.get_round(db, round_id, owner.id)
    session_id = round_.session_id
    rounds_service.undo(db, round_)
    session = sessions_service.get_session_by_id(db, session_id)
    return _build_current(db, session, request)


def member_page_url(request: Request, session_token: str) -> str:
    """メンバー用画面の URL。

    既定ではブラウザが実際に叩いたホストから組み立てる。手元の LAN の IP でも
    Vercel のドメインでも、そのまま読み取れる URL になるため。

    ただしサーバと同じ PC で ``localhost`` として開いていると、その URL は
    スマートフォンから届かない。そのために ``PUBLIC_BASE_URL`` で上書きできる。
    """
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    return f"{base}/member.html?session={session_token}"


@router.get("/sessions/{session_token}/member-qr.svg")
def member_qr(session_token: str, request: Request, db: DbSession) -> Response:
    """メンバー用画面の QR コード。全体表示画面に出して、各自のスマホで読んでもらう。"""
    sessions_service.get_session(db, session_token)
    code = segno.make(member_page_url(request, session_token), error="m")
    # <img> から読むので、名前空間付きの独立した SVG 文書として出力する
    # （svg_inline は HTML に直接埋め込む用で xmlns が付かず、画像として読めない）。
    buffer = BytesIO()
    code.save(buffer, kind="svg", scale=4, border=2, dark="#f2f5f8", light="#0f1216")
    return Response(
        content=buffer.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/sessions/{session_token}/stats", response_model=StatsOut)
def get_stats(session_token: str, db: DbSession) -> StatsOut:
    session = sessions_service.get_session(db, session_token)
    return StatsOut(
        adopted_rounds=len(stats_service.adopted_rounds(db, session.id)),
        play_counts=stats_service.play_counts(db, session.id),
    )
