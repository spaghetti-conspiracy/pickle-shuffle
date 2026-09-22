"""ラウンドの生成・採用・不採用・取り消し。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.config import settings as default_settings
from app.errors import ConflictError, NotEnoughPlayersError, NotFoundError
from app.models import (
    Court,
    Match,
    MatchSlot,
    Member,
    PracticeSession,
    Round,
    RoundParticipation,
    utcnow,
)
from app.scheduler.domain import MemberStatus, ParticipationState, RoundStatus
from app.scheduler.generator import generate_round, make_rng
from app.services import stats


def courts_in_use(session: PracticeSession) -> list[Court]:
    """試合に使うコート。練習用に外したコートは含まない。"""
    return [court for court in session.courts if court.in_use]


def current_round(db: Session, session_id: int) -> Round | None:
    """画面に出すべきラウンド。生成済みの pending があればそれ、無ければ直近の採用済み。"""
    pending = db.scalars(
        select(Round)
        .where(Round.session_id == session_id, Round.status == RoundStatus.PENDING)
        .order_by(Round.id.desc())
    ).first()
    if pending is not None:
        return pending
    return db.scalars(
        select(Round)
        .where(Round.session_id == session_id, Round.status == RoundStatus.ADOPTED)
        .order_by(Round.seq.desc())
    ).first()


def _last_adopted_id(db: Session, session_id: int) -> int:
    last = db.scalars(
        select(Round.id)
        .where(Round.session_id == session_id, Round.status == RoundStatus.ADOPTED)
        .order_by(Round.id.desc())
    ).first()
    return last or 0


def _rejected_since_last_adopt(db: Session, session_id: int) -> list[Round]:
    """直近の採用より後に不採用にしたラウンド。再生成の差別化に使う。"""
    return list(
        db.scalars(
            select(Round)
            .where(
                Round.session_id == session_id,
                Round.status == RoundStatus.REJECTED,
                Round.id > _last_adopted_id(db, session_id),
            )
            .order_by(Round.id)
        )
    )


def _signature(round_: Round) -> tuple:
    """コートの入れ替えを無視した編成の署名。"""
    match_sigs = []
    for match in round_.matches:
        teams: dict[int, list[int]] = {}
        for slot in match.slots:
            teams.setdefault(slot.team_index, []).append(slot.member_id)
        sides = [tuple(sorted(team)) for team in teams.values()]
        match_sigs.append(tuple(sorted(sides)))
    return tuple(sorted(match_sigs))


def generate(
    db: Session,
    session: PracticeSession,
    config: Settings | None = None,
) -> Round:
    """次のラウンドを生成して pending として保存する。

    生成に使うのは「この瞬間のメンバーとコートの状態」。すでに pending があれば
    不採用にしてから作り直す。メンバーやコートの変更を過去の pending に
    さかのぼって反映することはしない（CLAUDE.md 不変則12）。
    """
    config = config or default_settings

    available = courts_in_use(session)
    if not available:
        raise NotEnoughPlayersError("試合に使えるコートがありません")

    for pending in db.scalars(
        select(Round).where(
            Round.session_id == session.id, Round.status == RoundStatus.PENDING
        )
    ):
        pending.status = RoundStatus.REJECTED
        pending.decided_at = utcnow()
    db.flush()

    rejected = _rejected_since_last_adopt(db, session.id)
    attempt = len(rejected)
    round_seq = len(stats.adopted_rounds(db, session.id)) + 1

    plan = generate_round(
        stats.build_player_stats(db, session.id),
        stats.build_history(db, session.id),
        court_count=len(available),
        seed=session.random_seed,
        rng=make_rng(session.random_seed, round_seq, attempt),
        fairness_slack=config.fairness_slack,
        max_candidate_sets=config.max_candidate_sets,
        lookahead=config.lookahead,
        beam=config.beam,
        weights=config.weights,
        avoid=tuple(_signature(r) for r in rejected),
    )

    round_ = Round(session_id=session.id, status=RoundStatus.PENDING, attempt=attempt)
    db.add(round_)
    db.flush()

    for match_plan in plan.matches:
        # 生成器は「使えるコートの何番目か」しか知らないので、ここで実物に割り当てる。
        match = Match(round_id=round_.id, court_id=available[match_plan.court_index].id)
        db.add(match)
        db.flush()
        for team_index, team in enumerate((match_plan.team_a, match_plan.team_b)):
            for member_id in team:
                db.add(
                    MatchSlot(match_id=match.id, team_index=team_index, member_id=member_id)
                )

    db.commit()
    db.refresh(round_)
    return round_


def adopt(db: Session, round_: Round) -> Round:
    """採用する（=「開始」）。この時点で初めて統計に反映される。"""
    if round_.status is not RoundStatus.PENDING:
        raise ConflictError("このマッチはすでに決定済みです")

    playing = {slot.member_id for match in round_.matches for slot in match.slots}
    members = db.scalars(
        select(Member).where(
            Member.session_id == round_.session_id, Member.status != MemberStatus.LEFT
        )
    ).all()

    for member in members:
        if member.id in playing:
            state = ParticipationState.PLAYED
        elif member.status is MemberStatus.RESTING:
            state = ParticipationState.RESTING
        else:
            state = ParticipationState.SAT_OUT
        db.add(
            RoundParticipation(round_id=round_.id, member_id=member.id, state=state)
        )

    round_.status = RoundStatus.ADOPTED
    round_.seq = len(stats.adopted_rounds(db, round_.session_id)) + 1
    round_.decided_at = utcnow()
    db.commit()
    db.refresh(round_)
    return round_


def reject(db: Session, round_: Round) -> Round:
    """不採用にする（=「スキップ」）。統計には一切影響しない。"""
    if round_.status is not RoundStatus.PENDING:
        raise ConflictError("このマッチはすでに決定済みです")
    round_.status = RoundStatus.REJECTED
    round_.decided_at = utcnow()
    db.commit()
    db.refresh(round_)
    return round_


def undo(db: Session, round_: Round) -> None:
    """直近の採用を取り消す。

    統計はスナップショットから導出しているので、ラウンドごと消せば元に戻る。
    """
    if round_.status is not RoundStatus.ADOPTED:
        raise ConflictError("採用済みのマッチではありません")
    latest = db.scalars(
        select(Round)
        .where(Round.session_id == round_.session_id, Round.status == RoundStatus.ADOPTED)
        .order_by(Round.seq.desc())
    ).first()
    if latest is None or latest.id != round_.id:
        raise ConflictError("取り消せるのは最後に開始したマッチだけです")
    db.delete(round_)
    db.commit()


def get_round(db: Session, round_id: int) -> Round:
    """ラウンドを取得する。"""
    round_ = db.get(Round, round_id)
    if round_ is None:
        raise NotFoundError("マッチが見つかりません")
    return round_
