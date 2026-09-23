"""練習会・コート・メンバーの操作。"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Court,
    Match,
    MatchSlot,
    Member,
    MemberProfile,
    PracticeSession,
    RoundParticipation,
    default_court_name,
    new_random_seed,
    utcnow,
)
from app.scheduler.domain import Gender, Level, MemberStatus
from app.services import stats

MAX_COURTS = 4
"""コート数の上限。実際の練習会で押さえられる面数から決めた。"""


# ---------------------------------------------------------------------------
# 練習会とコート
# ---------------------------------------------------------------------------


def create_session(db: Session, name: str, court_count: int = 2) -> PracticeSession:
    """練習会を作る。コートは最大数ぶんまとめて作る。"""
    if not name.strip():
        raise ValidationError("練習会の名前を入力してください")
    if not 1 <= court_count <= MAX_COURTS:
        raise ValidationError(f"コート数は1〜{MAX_COURTS}の範囲で指定してください")

    session = PracticeSession(name=name.strip(), random_seed=new_random_seed())
    db.add(session)
    db.flush()
    for index in range(court_count):
        db.add(
            Court(session_id=session.id, court_index=index, name=default_court_name(index))
        )
    db.commit()
    db.refresh(session)
    return session


def get_session(db: Session, token: str) -> PracticeSession:
    """URL のトークンから練習会を引く。

    連番の id は外に出さないので、外から来る識別子は必ずトークン。
    """
    session = db.scalars(
        select(PracticeSession).where(PracticeSession.token == token)
    ).first()
    if session is None:
        raise NotFoundError("練習会が見つかりません")
    return session


def get_session_by_id(db: Session, session_id: int) -> PracticeSession:
    """内部 id から引く。ラウンドなど、すでに手元に id がある場合だけ使う。"""
    session = db.get(PracticeSession, session_id)
    if session is None:
        raise NotFoundError("練習会が見つかりません")
    return session


def list_sessions(db: Session) -> list[PracticeSession]:
    return list(db.scalars(select(PracticeSession).order_by(PracticeSession.id.desc())))


def update_session(
    db: Session,
    session: PracticeSession,
    *,
    name: str | None = None,
    highlight_beginners: bool | None = None,
) -> PracticeSession:
    if name is not None:
        if not name.strip():
            raise ValidationError("練習会の名前を入力してください")
        session.name = name.strip()
    if highlight_beginners is not None:
        session.highlight_beginners = highlight_beginners
    db.commit()
    db.refresh(session)
    return session


def delete_session(db: Session, session: PracticeSession) -> None:
    """記録ごと破棄する。ニックネームの辞書は練習会に属さないので残る。"""
    db.delete(session)
    db.commit()


def update_court(
    db: Session,
    session: PracticeSession,
    court_id: int,
    *,
    name: str | None = None,
    in_use: bool | None = None,
) -> Court:
    """コートの名前と、試合に使うかどうかを変える。

    初心者の育成用に1面を練習コートとして空ける、といった使い方を想定している。
    試合から外れるメンバーは休憩にしておけばよい。
    """
    court = next((c for c in session.courts if c.id == court_id), None)
    if court is None:
        raise NotFoundError("コートが見つかりません")

    if name is not None:
        if not name.strip():
            raise ValidationError("コート名を入力してください")
        court.name = name.strip()

    if in_use is not None:
        if not in_use and sum(1 for c in session.courts if c.in_use and c.id != court.id) == 0:
            raise ConflictError("すべてのコートを試合から外すことはできません")
        court.in_use = in_use

    db.commit()
    db.refresh(court)
    return court


# ---------------------------------------------------------------------------
# メンバー
# ---------------------------------------------------------------------------


def _upsert_profile(db: Session, nickname: str, gender: Gender, level: Level) -> None:
    """ニックネームの辞書を更新する。

    属性だけを覚えておいて次の練習会で使い回す。統計は共有しない（不変則13）。
    同名が複数いても後勝ちでよい、という運用方針。
    """
    profile = db.scalars(
        select(MemberProfile).where(MemberProfile.nickname == nickname)
    ).first()
    if profile is None:
        db.add(MemberProfile(nickname=nickname, gender=gender, level=level))
    else:
        profile.gender = gender
        profile.level = level
        profile.updated_at = utcnow()


def list_profiles(db: Session) -> list[MemberProfile]:
    return list(db.scalars(select(MemberProfile).order_by(MemberProfile.nickname)))


def delete_profile(db: Session, nickname: str) -> None:
    profile = db.scalars(
        select(MemberProfile).where(MemberProfile.nickname == nickname)
    ).first()
    if profile is None:
        raise NotFoundError("登録がありません")
    db.delete(profile)
    db.commit()


def add_member(
    db: Session,
    session: PracticeSession,
    *,
    nickname: str,
    gender: Gender,
    level: Level,
) -> Member:
    """メンバーを登録する。途中参加でも公平になるよう下駄を履かせる。"""
    if not nickname.strip():
        raise ValidationError("ニックネームを入力してください")
    nickname = nickname.strip()

    # 下駄は参加時点の active メンバーの最小 adjusted。これが無いと
    # 遅刻者が追いつくまで何ラウンドも連続出場してしまう。
    actives = [
        p
        for p in stats.build_player_stats(db, session.id)
        if p.status is MemberStatus.ACTIVE
    ]
    baseline = min((p.adjusted for p in actives), default=0)

    member = Member(
        session_id=session.id,
        nickname=nickname,
        gender=gender,
        level=level,
        baseline=baseline,
    )
    db.add(member)
    _upsert_profile(db, nickname, gender, level)
    db.commit()
    db.refresh(member)
    return member


def get_member(db: Session, member_id: int) -> Member:
    member = db.get(Member, member_id)
    if member is None:
        raise NotFoundError("メンバーが見つかりません")
    return member


def update_member(
    db: Session,
    member: Member,
    *,
    nickname: str | None = None,
    gender: Gender | None = None,
    level: Level | None = None,
    status: MemberStatus | None = None,
) -> Member:
    """メンバーの属性や状態を変える。休憩・復帰もここ。

    変更は生成済みのマッチにはさかのぼらない。反映は次の生成から（不変則12）。
    """
    if nickname is not None:
        if not nickname.strip():
            raise ValidationError("ニックネームを入力してください")
        member.nickname = nickname.strip()
    if gender is not None:
        member.gender = gender
    if level is not None:
        member.level = level
    if status is not None:
        member.status = status

    _upsert_profile(db, member.nickname, member.gender, member.level)
    db.commit()
    db.refresh(member)
    return member


def remove_member(db: Session, member: Member) -> None:
    """メンバーを外す。

    どのラウンドにも登場していなければ本当に消す。登場していれば離脱扱いにして
    記録を残す（過去のマッチの表示が壊れないように）。
    """
    appeared = db.scalars(
        select(MatchSlot.id).where(MatchSlot.member_id == member.id)
    ).first()
    recorded = db.scalars(
        select(RoundParticipation.id).where(RoundParticipation.member_id == member.id)
    ).first()
    if appeared is None and recorded is None:
        db.delete(member)
    else:
        member.status = MemberStatus.LEFT
    db.commit()


def list_members(db: Session, session_id: int) -> list[Member]:
    return list(
        db.scalars(
            select(Member).where(Member.session_id == session_id).order_by(Member.id)
        )
    )


def duplicate_nicknames(members: list[Member]) -> set[str]:
    """同じ練習会に同名が複数いるか。禁止はせず、警告のために数えるだけ。"""
    counts = Counter(m.nickname for m in members if m.status is not MemberStatus.LEFT)
    return {nickname for nickname, count in counts.items() if count > 1}


def match_duplicate_nicknames(db: Session, round_id: int | None) -> list[str]:
    """表示中のラウンドに同名が2人以上いるか。どちらか分からなくなるので警告する。"""
    if round_id is None:
        return []
    names = db.execute(
        select(Member.nickname)
        .join(MatchSlot, MatchSlot.member_id == Member.id)
        .join(Match, Match.id == MatchSlot.match_id)
        .where(Match.round_id == round_id)
    ).scalars().all()
    counts = Counter(names)
    return sorted(nickname for nickname, count in counts.items() if count > 1)
