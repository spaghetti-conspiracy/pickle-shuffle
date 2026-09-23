"""ラウンドの生成・採用・不採用・取り消し。"""

from __future__ import annotations

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
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
    TimerState,
    as_utc,
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


def _claim(db: Session, round_: Round, expected: RoundStatus, new: RoundStatus) -> bool:
    """状態を条件付きで書き換え、実際に自分が遷移させたかを返す。

    在メモリの ``round_.status`` を見てから書くと、別の端末が先にコミット
    していても素通りしてしまう。管理画面と全体表示画面は別端末から同時に
    使われる前提なので、WHERE で今の状態を縛り、更新できた行数で判定する。
    """
    result = db.execute(
        update(Round)
        .where(Round.id == round_.id, Round.status == expected)
        .values(status=new, decided_at=utcnow())
    )
    return result.rowcount == 1


def adopt(db: Session, round_: Round) -> Round:
    """採用する（=「開始」）。この時点で初めて統計に反映される。"""
    if round_.status is not RoundStatus.PENDING:
        raise ConflictError("このマッチはすでに決定済みです")

    seq = len(stats.adopted_rounds(db, round_.session_id)) + 1
    if not _claim(db, round_, RoundStatus.PENDING, RoundStatus.ADOPTED):
        # 別の端末が先に開始またはスキップした。
        db.rollback()
        raise ConflictError("このマッチはすでに決定済みです")
    round_.seq = seq

    # 同じ練習会に pending が残っていたら、もう画面には出さない。
    # 生成が同時に走ると pending が2本できることがあり、残したままだと
    # 開始したマッチが隠れて、しかも二重に採用できてしまう。
    db.execute(
        update(Round)
        .where(
            Round.session_id == round_.session_id,
            Round.status == RoundStatus.PENDING,
            Round.id != round_.id,
        )
        .values(status=RoundStatus.REJECTED, decided_at=utcnow())
    )

    playing = {slot.member_id for match in round_.matches for slot in match.slots}
    members = db.scalars(
        select(Member).where(
            Member.session_id == round_.session_id,
            # 出場中に削除された人も、そのラウンドの記録としては残す。
            # 外すと MatchSlot だけが残り、当時のレベルが引けなくなる（不変則2）。
            or_(Member.status != MemberStatus.LEFT, Member.id.in_(playing)),
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
            RoundParticipation(
                round_id=round_.id,
                member_id=member.id,
                state=state,
                # そのときのレベルを残す。あとで変更されても履歴は動かない。
                level=member.level,
            )
        )

    # 開始と同時に試合時計を動かす。
    round_.timer_state = TimerState.RUNNING
    round_.timer_started_at = utcnow()
    round_.timer_elapsed_seconds = 0

    try:
        db.commit()
    except IntegrityError as error:
        # 別の端末が同じラウンドを採用しきった、または同じ seq を取った。
        db.rollback()
        raise ConflictError("このマッチはすでに決定済みです") from error
    db.refresh(round_)
    return round_


def reject(db: Session, round_: Round) -> Round:
    """不採用にする（=「スキップ」）。統計には一切影響しない。"""
    if round_.status is not RoundStatus.PENDING:
        raise ConflictError("このマッチはすでに決定済みです")
    if not _claim(db, round_, RoundStatus.PENDING, RoundStatus.REJECTED):
        db.rollback()
        raise ConflictError("このマッチはすでに決定済みです")
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
    result = db.execute(
        delete(Round).where(Round.id == round_.id, Round.status == RoundStatus.ADOPTED)
    )
    if result.rowcount != 1:
        db.rollback()
        raise ConflictError("このマッチはすでに取り消されています")
    # 取り消したラウンドの統計から作られた pending は、もう根拠を失っている。
    db.execute(
        update(Round)
        .where(Round.session_id == round_.session_id, Round.status == RoundStatus.PENDING)
        .values(status=RoundStatus.REJECTED, decided_at=utcnow())
    )
    db.commit()


def get_round(db: Session, round_id: int) -> Round:
    """ラウンドを取得する。"""
    round_ = db.get(Round, round_id)
    if round_ is None:
        raise NotFoundError("マッチが見つかりません")
    return round_


# ---------------------------------------------------------------------------
# 試合時計
#
# 締切の時刻ではなく「経過した秒」を持つ。こうしておくと、試合の途中で
# 持ち時間の設定を変えても、その場で正しい残り時間になる。
# ---------------------------------------------------------------------------

TIMER_MIN_MINUTES = 3
TIMER_MAX_MINUTES = 15


def elapsed_seconds(round_: Round) -> int:
    """その試合が始まってから動いていた秒数。止めている間は増えない。"""
    total = round_.timer_elapsed_seconds
    if round_.timer_state is TimerState.RUNNING and round_.timer_started_at is not None:
        total += int((utcnow() - as_utc(round_.timer_started_at)).total_seconds())
    return max(total, 0)


def start_timer(db: Session, round_: Round) -> Round:
    """時計を動かし始める。採用（=開始）と同時に呼ぶ。"""
    round_.timer_state = TimerState.RUNNING
    round_.timer_started_at = utcnow()
    round_.timer_elapsed_seconds = 0
    db.commit()
    db.refresh(round_)
    return round_


def pause_timer(db: Session, round_: Round) -> Round:
    """一時停止。経過を積んで止める。あとで再開できる。"""
    if round_.timer_state is not TimerState.RUNNING:
        raise ConflictError("動いている時計がありません")
    round_.timer_elapsed_seconds = elapsed_seconds(round_)
    round_.timer_started_at = None
    round_.timer_state = TimerState.PAUSED
    db.commit()
    db.refresh(round_)
    return round_


def resume_timer(db: Session, round_: Round) -> Round:
    """一時停止から再開する。"""
    if round_.timer_state is not TimerState.PAUSED:
        raise ConflictError("止まっている時計がありません")
    round_.timer_started_at = utcnow()
    round_.timer_state = TimerState.RUNNING
    db.commit()
    db.refresh(round_)
    return round_


def silence_alarm(db: Session, round_: Round) -> Round:
    """時間切れの音を止める。どの端末で押しても全員で止まる。"""
    round_.timer_alarm_silenced = True
    db.commit()
    db.refresh(round_)
    return round_


def stop_timer(db: Session, round_: Round) -> Round:
    """中断する。一時停止と違い、この試合の時計はもう表示しない。

    試合を打ち切って次へ進むときに使う。
    """
    round_.timer_elapsed_seconds = elapsed_seconds(round_)
    round_.timer_started_at = None
    round_.timer_state = TimerState.STOPPED
    round_.timer_alarm_silenced = True  # 中断したのに鳴り続けない
    db.commit()
    db.refresh(round_)
    return round_
