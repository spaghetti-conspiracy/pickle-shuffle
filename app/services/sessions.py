"""練習会・コート・メンバーの操作。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    NICKNAME_MAX,
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
from app.tennisbear import Participant

MAX_COURTS = 4
"""コート数の上限。実際の練習会で押さえられる面数から決めた。"""




# ---------------------------------------------------------------------------
# 練習会とコート
# ---------------------------------------------------------------------------


def _reject_duplicate_name(db: Session, name: str, *, exclude_id: int | None = None) -> None:
    """同じ名前の練習会があれば断る。

    選択画面はプルダウンに名前だけを出すので、同名だと見分けられない。
    """
    query = select(PracticeSession).where(PracticeSession.name == name)
    if exclude_id is not None:
        query = query.where(PracticeSession.id != exclude_id)
    if db.scalars(query).first() is not None:
        raise ValidationError(
            f"「{name}」という練習会がすでにあります。"
            "終了させるか、別の名前にしてください。"
        )


def create_session(db: Session, name: str, court_count: int = 2) -> PracticeSession:
    """練習会を作る。コートは最大数ぶんまとめて作る。"""
    if not name.strip():
        raise ValidationError("練習会の名前を入力してください")
    if not 1 <= court_count <= MAX_COURTS:
        raise ValidationError(f"コート数は1〜{MAX_COURTS}の範囲で指定してください")
    name = name.strip()
    _reject_duplicate_name(db, name)

    session = PracticeSession(name=name, random_seed=new_random_seed())
    db.add(session)
    db.flush()
    for index in range(court_count):
        db.add(
            Court(session_id=session.id, court_index=index, name=default_court_name(index))
        )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if "uq_session_name" not in str(error.orig):
            # 名前以外の一意制約。言い換えると原因の特定を妨げる。
            raise
        # 事前の確認とコミットの間に、別の端末が同じ名前で作った。
        raise ValidationError(
            f"「{name}」という練習会がすでにあります。"
            "終了させるか、別の名前にしてください。"
        ) from error
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
        _reject_duplicate_name(db, name.strip(), exclude_id=session.id)
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


def find_profile(
    db: Session, *, nickname: str, tennisbear_user_id: int | None = None
) -> MemberProfile | None:
    """属性の辞書を引く。tennisbear の ID があればそちらを優先する。

    ニックネームは識別子ではない（不変則14）ので、改名されると
    名前では見失う。ID で引ければ、管理者が直したレベルが次の練習会にも残る。

    ID を持っている相手にニックネームで当てにいかない。同名の別人の属性を
    そのまま被ってしまう（「マッツ」を初心者に直したら、別の「マッツ」も
    初心者で入る）。名前で引くのは、ID の無い行に限る。
    """
    if tennisbear_user_id is not None:
        found = db.scalars(
            select(MemberProfile).where(
                MemberProfile.tennisbear_user_id == tennisbear_user_id
            )
        ).first()
        if found is not None:
            return found
    by_name = db.scalars(
        select(MemberProfile).where(MemberProfile.nickname == nickname)
    ).first()
    if by_name is None:
        return None
    if tennisbear_user_id is not None and by_name.tennisbear_user_id is not None:
        # 名前は同じだが、別の人の行だと分かっている。
        return None
    return by_name


def _upsert_profile(
    db: Session,
    nickname: str,
    gender: Gender,
    level: Level,
    tennisbear_user_id: int | None = None,
) -> None:
    """属性の辞書を更新する。

    属性だけを覚えておいて次の練習会で使い回す。統計は共有しない（不変則13）。
    同名が複数いても後勝ちでよい、という運用方針。
    """
    profile = find_profile(db, nickname=nickname, tennisbear_user_id=tennisbear_user_id)
    if profile is None:
        if db.scalars(
            select(MemberProfile).where(MemberProfile.nickname == nickname)
        ).first() is not None:
            # 同じ名前の別人の行がある。ニックネームは一意なので、ここで
            # 新しい行は作れない。属性の引き継ぎを諦めるだけで害はない。
            return
        db.add(
            MemberProfile(
                nickname=nickname,
                gender=gender,
                level=level,
                tennisbear_user_id=tennisbear_user_id,
            )
        )
        return
    # **名前は書き換えない。** ニックネームは一意なので、別の行とぶつかると
    # 取り込みが丸ごと失敗する。ここで覚えたいのは属性であって名前ではない。
    profile.gender = gender
    profile.level = level
    if tennisbear_user_id is not None:
        profile.tennisbear_user_id = tennisbear_user_id
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
    tennisbear_user_id: int | None = None,
    tennisbear_nickname: str | None = None,
    commit: bool = True,
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
        tennisbear_user_id=tennisbear_user_id,
        tennisbear_nickname=tennisbear_nickname,
    )
    db.add(member)
    _upsert_profile(db, nickname, gender, level, tennisbear_user_id)
    if not commit:
        # まとめて取り込むときは、最後に一度だけコミットする。
        # 1人ずつ確定すると、途中で失敗したときに中途半端に残る。
        db.flush()
        return member
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


# ---------------------------------------------------------------------------
# tennisbear からの取り込み
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportResult:
    """取り込みの結果。画面にそのまま出せる粒度で返す。"""

    added: list[str]
    renamed: list[tuple[str, str]]
    unchanged: int
    resting: list[str]
    """一覧から居なくなったので休憩にした人。削除はしない（統計が壊れる）。"""

    @property
    def total(self) -> int:
        return len(self.added) + len(self.renamed) + self.unchanged


def unique_nickname(base: str, taken: set[str]) -> str:
    """重複しないニックネームにする。先にいる人はそのまま、後の人に番号を振る。

    tennisbear の ID で区別はできるが、画面に出すには細かすぎる。
    「マッツ」「マッツ2」なら読み上げにも使える。番号はその練習会の中でだけ
    意味を持つ（不変則14: ニックネームは識別子ではない）。
    """
    base = base[:NICKNAME_MAX]
    if base not in taken:
        return base
    number = 2
    while True:
        suffix = str(number)
        candidate = base[: NICKNAME_MAX - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        number += 1


def check_event(session: PracticeSession, event_id: int) -> None:
    """取り込み元が食い違っていないかだけを見る。書き換えはしない。

    イベントページを取りに行く前に呼ぶ。打ち間違えたIDで外へ出ていくと、
    「見つかりませんでした」が先に返ってしまい、本当の理由（別のイベントは
    取り込めない）が伝わらない。
    """
    bound = session.tennisbear_event_id
    if bound is not None and bound != event_id:
        raise ValidationError(
            f"この練習会はイベント {bound} から取り込んでいます。別のイベントは取り込めません。"
        )


def _bind_event(db: Session, session: PracticeSession, event_id: int) -> None:
    """この練習会の取り込み元を、最初のイベントに確定する。

    読んでから書くと、2つの端末が同時に初めての取り込みを押したときに
    両方が通ってしまう。まだ紐づいていない行だけを WHERE で狙い、
    更新できた行数で「自分が確定させたか」を判定する。
    """
    if session.tennisbear_event_id is None:
        claimed = (
            db.execute(
                update(PracticeSession)
                .where(
                    PracticeSession.id == session.id,
                    PracticeSession.tennisbear_event_id.is_(None),
                )
                .values(tennisbear_event_id=event_id)
            ).rowcount
            == 1
        )
        if claimed:
            session.tennisbear_event_id = event_id
            return
        # 別の端末が先に確定させた。今の値で判定し直す。
        db.refresh(session)
    check_event(session, event_id)


def import_participants(
    db: Session,
    session: PracticeSession,
    participants: list[Participant],
    *,
    event_id: int | None,
) -> ImportResult:
    """イベントの参加者を練習会に取り込む。

    すでに取り込んだ人は tennisbear の ID で見分ける。そのときの扱いは:

    - **属性はこちらの DB を優先する。** 管理者が直したレベルや性別を、
      取り込みのたびに戻してしまわないため
    - **ニックネームだけは追従する。** 呼び名が変わったのに古い名前で
      読み上げると混乱する。変えるときも重複を避けて番号を振り直す
    - **いなくなった人は消さない。** 統計が壊れるので、手で「休憩」にしてもらう

    新しく入れる人の属性は、過去の練習会で覚えた値（`member_profiles`）が
    あればそちらを使う。無ければ tennisbear から推定した値を使う。

    **練習会に紐づくイベントは1つに縛る。** 別のイベントを取り込むと、
    その一覧に居ない人が一斉に休憩へ回る。イベントIDを打ち間違えたときに
    黙って起きると事故になる。
    """
    if event_id is not None:
        _bind_event(db, session, event_id)
    existing = list(
        db.scalars(select(Member).where(Member.session_id == session.id))
    )
    by_tennisbear = {
        member.tennisbear_user_id: member
        for member in existing
        if member.tennisbear_user_id is not None
    }
    taken = {member.nickname for member in existing}

    added: list[str] = []
    renamed: list[tuple[str, str]] = []
    unchanged = 0
    seen: set[int] = set()

    for participant in participants:
        if participant.user_id in seen:
            # 同じ人が2回出てくることがある（キャンセルして再申込など）。
            # 見落とすと幽霊メンバーができ、毎ラウンド出場枠を1つ食う。
            unchanged += 1
            continue
        seen.add(participant.user_id)
        member = by_tennisbear.get(participant.user_id)
        if member is None:
            profile = find_profile(
                db,
                nickname=participant.nickname,
                tennisbear_user_id=participant.user_id,
            )
            nickname = unique_nickname(participant.nickname, taken)
            member_row = add_member(
                db,
                session,
                nickname=nickname,
                gender=profile.gender if profile else participant.gender,
                level=profile.level if profile else participant.level,
                tennisbear_user_id=participant.user_id,
                tennisbear_nickname=participant.nickname,
                commit=False,
            )
            taken.add(nickname)
            added.append(nickname)
            by_tennisbear[participant.user_id] = member_row
            continue

        if member.tennisbear_nickname == participant.nickname:
            # 上流は変わっていない。手元で付け直した呼び名を尊重する。
            unchanged += 1
            continue
        member.tennisbear_nickname = participant.nickname
        wanted = unique_nickname(participant.nickname, taken - {member.nickname})
        if wanted != member.nickname:
            before = member.nickname
            taken.discard(before)
            member.nickname = wanted
            taken.add(wanted)
            renamed.append((before, wanted))
        else:
            unchanged += 1

    # 一覧から消えた人は休憩にする。削除すると統計が壊れる（仕様）。
    rested: list[str] = []
    for member in existing:
        if member.tennisbear_user_id is None or member.tennisbear_user_id in seen:
            continue
        if member.status is MemberStatus.ACTIVE:
            member.status = MemberStatus.RESTING
            rested.append(member.nickname)

    db.commit()
    return ImportResult(
        added=added, renamed=renamed, unchanged=unchanged, resting=rested
    )
