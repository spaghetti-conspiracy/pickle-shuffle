"""HTTP API。ドメインの例外は main.py で HTTP ステータスへ変換する。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Court, Member, PracticeSession, Round
from app.scheduler.domain import MemberStatus
from app.schemas import (
    CourtOut,
    CourtStateOut,
    CourtUpdate,
    CurrentOut,
    MatchOut,
    MemberCreate,
    MemberOut,
    MemberProfileOut,
    MemberUpdate,
    PlayerOut,
    SessionCreate,
    SessionOut,
    SessionUpdate,
    StatsOut,
)
from app.services import rounds as rounds_service
from app.services import sessions as sessions_service
from app.services import stats as stats_service

router = APIRouter(prefix="/api")

DbSession = Annotated[Session, Depends(get_db)]
"""リクエストごとの DB セッション。"""


@router.get("/health")
def health() -> dict[str, str]:
    """死活監視用。"""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 練習会
# ---------------------------------------------------------------------------


def _session_out(session: PracticeSession) -> SessionOut:
    return SessionOut(
        id=session.id,
        name=session.name,
        rotation=session.rotation,
        created_at=session.created_at,
        courts=[CourtOut.model_validate(c) for c in session.courts],
    )


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(db: DbSession) -> list[SessionOut]:
    return [_session_out(s) for s in sessions_service.list_sessions(db)]


@router.post("/sessions", response_model=SessionOut, status_code=201)
def create_session(payload: SessionCreate, db: DbSession) -> SessionOut:
    session = sessions_service.create_session(db, payload.name, payload.court_count)
    return _session_out(session)


@router.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: int, db: DbSession) -> SessionOut:
    return _session_out(sessions_service.get_session(db, session_id))


@router.patch("/sessions/{session_id}", response_model=SessionOut)
def update_session(
    session_id: int, payload: SessionUpdate, db: DbSession
) -> SessionOut:
    session = sessions_service.get_session(db, session_id)
    sessions_service.update_session(
        db, session, name=payload.name, rotation=payload.rotation
    )
    return _session_out(session)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, db: DbSession) -> None:
    sessions_service.delete_session(db, sessions_service.get_session(db, session_id))


@router.patch("/sessions/{session_id}/courts/{court_id}", response_model=CourtOut)
def update_court(
    session_id: int,
    court_id: int,
    payload: CourtUpdate,
    db: DbSession,
) -> CourtOut:
    """コート名の変更と、試合用から外す/戻す。"""
    session = sessions_service.get_session(db, session_id)
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
    )


@router.get("/sessions/{session_id}/members", response_model=list[MemberOut])
def list_members(session_id: int, db: DbSession) -> list[MemberOut]:
    sessions_service.get_session(db, session_id)
    plays = stats_service.play_counts(db, session_id)
    return [
        _member_out(m, plays) for m in sessions_service.list_members(db, session_id)
    ]


@router.post("/sessions/{session_id}/members", response_model=MemberOut, status_code=201)
def add_member(
    session_id: int, payload: MemberCreate, db: DbSession
) -> MemberOut:
    session = sessions_service.get_session(db, session_id)
    member = sessions_service.add_member(
        db,
        session,
        nickname=payload.nickname,
        gender=payload.gender,
        level=payload.level,
    )
    return _member_out(member, {})


@router.patch("/members/{member_id}", response_model=MemberOut)
def update_member(
    member_id: int, payload: MemberUpdate, db: DbSession
) -> MemberOut:
    member = sessions_service.get_member(db, member_id)
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
def remove_member(member_id: int, db: DbSession) -> None:
    sessions_service.remove_member(db, sessions_service.get_member(db, member_id))


@router.get("/member-profiles", response_model=list[MemberProfileOut])
def list_profiles(db: DbSession) -> list[MemberProfileOut]:
    """過去に登録した名前と属性。登録画面の入力補完に使う。"""
    return [MemberProfileOut.model_validate(p) for p in sessions_service.list_profiles(db)]


@router.delete("/member-profiles/{nickname}", status_code=204)
def delete_profile(nickname: str, db: DbSession) -> None:
    sessions_service.delete_profile(db, nickname)


# ---------------------------------------------------------------------------
# ラウンド
# ---------------------------------------------------------------------------


def _player_out(member: Member) -> PlayerOut:
    return PlayerOut(
        id=member.id,
        nickname=member.nickname,
        gender=member.gender,
        level=member.level,
    )


def _revision(round_: Round | None, courts: list[Court]) -> str:
    """ラウンドとコートの状態だけで決まる値。

    メンバーを編集しただけでは変わらないので、試合中に表示が勝手に動かない。
    """
    court_part = ",".join(f"{c.id}{int(c.in_use)}" for c in courts)
    if round_ is None:
        return f"-|{court_part}"
    return f"{round_.id}:{round_.status.value}|{court_part}"


def _build_current(db: Session, session: PracticeSession) -> CurrentOut:
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
                teams[slot.team_index].append(_player_out(member))
                playing.add(member.id)
            match_by_court[match.court_id] = MatchOut(team_a=teams[0], team_b=teams[1])

    court_states = []
    for court in session.courts:
        if court.id in match_by_court:
            state = "match"
        elif not court.in_use:
            state = "practice"
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
    stale = [
        m.nickname
        for m in members.values()
        if m.id in playing and m.status is not MemberStatus.ACTIVE
    ]

    return CurrentOut(
        session=_session_out(session),
        round_id=round_.id if round_ else None,
        round_status=round_.status if round_ else None,
        revision=_revision(round_, session.courts),
        courts=court_states,
        waiting=waiting,
        resting=resting,
        stale_members=sorted(stale),
        duplicate_nicknames=rounds_duplicate_names(db, round_),
    )


def rounds_duplicate_names(db: Session, round_: Round | None) -> list[str]:
    return sessions_service.match_duplicate_nicknames(db, round_.id if round_ else None)


@router.get("/sessions/{session_id}/current", response_model=CurrentOut)
def get_current(session_id: int, db: DbSession) -> CurrentOut:
    """表示画面が2秒おきに読むエンドポイント。"""
    return _build_current(db, sessions_service.get_session(db, session_id))


@router.post("/sessions/{session_id}/rounds/generate", response_model=CurrentOut)
def generate_round_api(session_id: int, db: DbSession) -> CurrentOut:
    session = sessions_service.get_session(db, session_id)
    rounds_service.generate(db, session)
    db.refresh(session)
    return _build_current(db, session)


@router.post("/rounds/{round_id}/adopt", response_model=CurrentOut)
def adopt_round(round_id: int, db: DbSession) -> CurrentOut:
    round_ = rounds_service.get_round(db, round_id)
    rounds_service.adopt(db, round_)
    return _build_current(db, sessions_service.get_session(db, round_.session_id))


@router.post("/rounds/{round_id}/reject", response_model=CurrentOut)
def reject_round(round_id: int, db: DbSession) -> CurrentOut:
    round_ = rounds_service.get_round(db, round_id)
    session_id = round_.session_id
    rounds_service.reject(db, round_)
    return _build_current(db, sessions_service.get_session(db, session_id))


@router.post("/rounds/{round_id}/undo", response_model=CurrentOut)
def undo_round(round_id: int, db: DbSession) -> CurrentOut:
    round_ = rounds_service.get_round(db, round_id)
    session_id = round_.session_id
    rounds_service.undo(db, round_)
    return _build_current(db, sessions_service.get_session(db, session_id))


@router.get("/sessions/{session_id}/stats", response_model=StatsOut)
def get_stats(session_id: int, db: DbSession) -> StatsOut:
    sessions_service.get_session(db, session_id)
    return StatsOut(
        adopted_rounds=len(stats_service.adopted_rounds(db, session_id)),
        play_counts=stats_service.play_counts(db, session_id),
    )
