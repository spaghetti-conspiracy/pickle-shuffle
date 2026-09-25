"""練習会・コート・メンバーの操作。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError, ValidationError
from app.external import SOURCE_TENNISBEAR, external_key, raw_id_of
from app.models import (
    Court,
    Match,
    MatchSlot,
    Member,
    Owner,
    Person,
    PracticeSession,
    RoundParticipation,
    default_court_name,
    new_random_seed,
)
from app.scheduler.domain import Gender, Level, MemberStatus, round_half_up
from app.services import people as people_service
from app.services import stats
from app.services.naming import unique_nickname
from app.tennisbear import Participant

MAX_COURTS = 4
"""コート数の上限。実際の練習会で押さえられる面数から決めた。"""




# ---------------------------------------------------------------------------
# 練習会とコート
# ---------------------------------------------------------------------------


def _reject_duplicate_name(
    db: Session, owner: Owner, name: str, *, exclude_id: int | None = None
) -> None:
    """同じ名前の練習会があれば断る。

    選択画面はプルダウンに名前だけを出すので、同名だと見分けられない。
    見分けがつかないのは同じ団体の中だけなので、判定も団体の中で行う。
    """
    query = select(PracticeSession).where(
        PracticeSession.owner_id == owner.id, PracticeSession.name == name
    )
    if exclude_id is not None:
        query = query.where(PracticeSession.id != exclude_id)
    if db.scalars(query).first() is not None:
        raise ValidationError(
            f"「{name}」という練習会がすでにあります。"
            "終了させるか、別の名前にしてください。"
        )


def create_session(
    db: Session, owner: Owner, name: str, court_count: int = 2
) -> PracticeSession:
    """練習会を作る。コートは最大数ぶんまとめて作る。"""
    if not name.strip():
        raise ValidationError("練習会の名前を入力してください")
    if not 1 <= court_count <= MAX_COURTS:
        raise ValidationError(f"コート数は1〜{MAX_COURTS}の範囲で指定してください")
    name = name.strip()
    _reject_duplicate_name(db, owner, name)

    session = PracticeSession(
        owner_id=owner.id, name=name, random_seed=new_random_seed()
    )
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
    トークンは全体で一意な capability なので、ここでは団体で絞らない
    （QR を読んだメンバーは合言葉を持っていない）。
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


def owner_of(db: Session, session: PracticeSession) -> Owner:
    """その練習会を持つ団体。

    トークンで開く画面は合言葉を持っていないので、団体は練習会から辿る。
    """
    owner = db.get(Owner, session.owner_id)
    if owner is None:
        raise NotFoundError("団体が見つかりません")
    return owner


def list_sessions(db: Session, owner: Owner) -> list[PracticeSession]:
    """選択画面に出す一覧。ほかの団体の練習会は出さない。"""
    return list(
        db.scalars(
            select(PracticeSession)
            .where(PracticeSession.owner_id == owner.id)
            .order_by(PracticeSession.id.desc())
        )
    )


def update_session(
    db: Session,
    session: PracticeSession,
    *,
    name: str | None = None,
    highlight_beginners: bool | None = None,
    timer_minutes: int | None = None,
    unlimited: bool = False,
) -> PracticeSession:
    if name is not None:
        if not name.strip():
            raise ValidationError("練習会の名前を入力してください")
        _reject_duplicate_name(db, owner_of(db, session), name.strip(), exclude_id=session.id)
        session.name = name.strip()
    if highlight_beginners is not None:
        session.highlight_beginners = highlight_beginners
    if unlimited:
        session.timer_minutes = None
    elif timer_minutes is not None:
        session.timer_minutes = timer_minutes
    db.commit()
    db.refresh(session)
    return session


def delete_session(db: Session, session: PracticeSession) -> None:
    """記録ごと破棄する。メンバー台帳は練習会に属さないので残る。"""
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


def add_member(
    db: Session,
    session: PracticeSession,
    person: Person,
    *,
    by_import: bool = False,
    commit: bool = True,
) -> Member:
    """台帳の人を、この練習会の参加者に加える。

    属性は台帳から**写す**。参照ではなく写しにするのは、台帳から人が消えても
    参加者と過去の記録が無傷で残るようにするため。台帳を直したときは
    `people.update_person` が書き写す（不変則12: 反映は次の生成から）。

    途中参加でも公平になるよう下駄を履かせる。
    """
    already = db.scalars(
        select(Member).where(
            Member.session_id == session.id, Member.person_id == person.id
        )
    ).first()
    if already is not None and already.status is not MemberStatus.LEFT:
        # 二重に入ると、同じ人が別のコートの2試合に同時に割り当てられ得る。
        raise ValidationError(f"「{already.nickname}」はすでにこの練習会に入っています")

    # 下駄は参加時点の active メンバーの最小 adjusted（四捨五入した値）。これが無いと
    # 遅刻者が追いつくまで何ラウンドも連続出場してしまう。
    actives = [
        p
        for p in stats.build_player_stats(db, session.id)
        if p.status is MemberStatus.ACTIVE
    ]
    # 生成も四捨五入した値で出場者を選ぶ。下駄は整数のまま保存できる。
    baseline = min((p.adjusted_rounded for p in actives), default=0)

    if already is not None:
        # 一度外した人が戻ってきた。**新しい行は作らず、離脱した行を戻す。**
        # 別の行にすると同じ人の記録が2つに割れ、それまでの出場が無かった
        # ことになって、その人だけ連続出場することになる。
        # 下駄は、戻った時点で adjusted がちょうど最小値になるように決める。
        # 離脱前の出場回数や休憩のみなし出場は記録に残っているので、その分を差し引く。
        # 下駄を最小値そのものにすると、離脱前の出場回数が上乗せされ、
        # その回数と同じくらいのラウンド数だけ出番が回らなくなる。
        # 離脱前の休憩で積み上がった不足は帳消しになる（途中参加と同じ扱い。ユーザー判断）。
        already.status = MemberStatus.ACTIVE
        db.flush()
        own = next(p for p in stats.build_player_stats(db, session.id) if p.id == already.id)
        # みなし出場は同じ四捨五入で差し引く。adjusted_rounded がちょうど最小値になる
        # （端数の残り d は [-0.5, 0.5) なので、四捨五入すると 0 になる）。
        already.baseline = baseline - own.plays - round_half_up(own.rest_credit)
        already.joined_by_import = by_import
        if commit:
            db.commit()
            db.refresh(already)
        return already

    taken = {
        member.nickname
        for member in list_members(db, session.id)
        if member.status is not MemberStatus.LEFT
    }
    member = Member(
        session_id=session.id,
        person_id=person.id,
        nickname=unique_nickname(person.nickname, taken),
        gender=person.gender,
        level=person.level,
        baseline=baseline,
        joined_by_import=by_import,
    )
    db.add(member)
    if not commit:
        # まとめて取り込むときは、最後に一度だけコミットする。
        # 1人ずつ確定すると、途中で失敗したときに中途半端に残る。
        db.flush()
        return member
    db.commit()
    db.refresh(member)
    return member


def get_member(db: Session, member_id: int, owner: Owner | None = None) -> Member:
    """参加者を id で引く。

    `owner` を渡すと、その団体のものかを確かめる。連番の id を外から渡せる
    endpoint は、ここで団体を確かめないと、よその団体の行に手が届いてしまう。
    """
    member = db.get(Member, member_id)
    if member is None:
        raise NotFoundError("メンバーが見つかりません")
    if owner is not None and owner_of(db, get_session_by_id(db, member.session_id)).id != owner.id:
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

    # 属性は台帳にも上げる。次の練習会でも直した値が使われるように。
    # **ほかの練習会には降ろさない。** 進行中の別の練習会の表示が、
    # こちらの操作で勝手に変わらないようにするため（不変則12）。
    # **名前は上げない。** 練習会の中での番号付けや読み上げ用の言い換えで、
    # 台帳の名前を書き換えてしまわないため。
    people_service.sync_from_member(db, member)
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
    unchanged: int
    resting: list[str]
    """一覧から居なくなったので休憩にした人。削除はしない（統計が壊れる）。"""

    @property
    def total(self) -> int:
        return len(self.added) + self.unchanged


def check_event(session: PracticeSession, event_key: str) -> None:
    """取り込み元が食い違っていないかだけを見る。書き換えはしない。

    イベントページを取りに行く前に呼ぶ。打ち間違えたIDで外へ出ていくと、
    「見つかりませんでした」が先に返ってしまい、本当の理由（別のイベントは
    取り込めない）が伝わらない。
    """
    bound = session.external_event_id
    if bound is not None and bound != event_key:
        raise ValidationError(
            f"この練習会はイベント {raw_id_of(bound)} から取り込んでいます。"
            "別のイベントは取り込めません。"
        )


def _bind_event(db: Session, session: PracticeSession, event_key: str) -> None:
    """この練習会の取り込み元を、最初のイベントに確定する。

    読んでから書くと、2つの端末が同時に初めての取り込みを押したときに
    両方が通ってしまう。まだ紐づいていない行だけを WHERE で狙い、
    更新できた行数で「自分が確定させたか」を判定する。
    """
    if session.external_event_id is None:
        claimed = (
            db.execute(
                update(PracticeSession)
                .where(
                    PracticeSession.id == session.id,
                    PracticeSession.external_event_id.is_(None),
                )
                .values(external_event_id=event_key)
            ).rowcount
            == 1
        )
        if claimed:
            session.external_event_id = event_key
            return
        # 別の端末が先に確定させた。今の値で判定し直す。
        db.refresh(session)
    check_event(session, event_key)


def import_participants(
    db: Session,
    owner: Owner,
    session: PracticeSession,
    participants: list[Participant],
    *,
    event_id: int | None,
) -> ImportResult:
    """イベントの参加者を練習会に取り込む。

    照合の軸は**メンバー台帳**。参加者はまず台帳の人に落としてから、
    その人が練習会に居るかを見る。扱いは:

    - **向こうと繋がっているのはユーザ ID だけ。** 名前・性別・レベルを
      写すのは、その人を初めて台帳に載せるときだけ。以後は一切同期しない。
      同名で番号が付いた人はたいてい別の呼び名に変えたくなるので、
      こちらで通じる名前を持つ方が自然であり、上流の都合で書き換わらない
    - **いなくなった人は消さない。** 統計が壊れるので、手で「休憩」にしてもらう

    台帳に居ない人は、取り込んだ値を下書きとして新しく登録する。
    推定したレベルは外れる前提で、管理者が直す。

    **練習会に紐づくイベントは1つに縛る。** 別のイベントを取り込むと、
    その一覧に居ない人が一斉に休憩へ回る。イベントIDを打ち間違えたときに
    黙って起きると事故になる。
    """
    if event_id is not None:
        _bind_event(db, session, external_key(SOURCE_TENNISBEAR, event_id))
    existing = list_members(db, session.id)
    by_person = {
        member.person_id: member
        for member in existing
        if member.person_id is not None
    }

    added: list[str] = []
    unchanged = 0
    seen: set[int] = set()

    for participant in participants:
        key = external_key(SOURCE_TENNISBEAR, participant.user_id)
        person = people_service.find_by_external(db, owner, key)
        if person is None:
            # 初めて見る人だけ、向こうの値を下書きとして写す。
            person = people_service.add_person(
                db,
                owner,
                nickname=participant.nickname,
                gender=participant.gender,
                level=participant.level,
                external_id=key,
                commit=False,
            )

        if person.id in seen:
            # 同じ人が2回出てくることがある（キャンセルして再申込など）。
            # 見落とすと幽霊メンバーができ、毎ラウンド出場枠を1つ食う。
            unchanged += 1
            continue
        seen.add(person.id)

        member = by_person.get(person.id)
        if member is None:
            member = add_member(db, session, person, by_import=True, commit=False)
            by_person[person.id] = member
            added.append(member.nickname)
        else:
            unchanged += 1

    # 一覧から消えた人は休憩にする。削除すると統計が壊れる（仕様）。
    # **手で足した人は対象にしない。** 先週取り込んだ人を今週は手で足す、という
    # ことがあるので、台帳に取り込み元があるかどうかでは判定できない。
    rested: list[str] = []
    for member in existing:
        if not member.joined_by_import or member.person_id in seen:
            continue
        if member.status is MemberStatus.ACTIVE:
            member.status = MemberStatus.RESTING
            rested.append(member.nickname)

    db.commit()
    return ImportResult(added=added, unchanged=unchanged, resting=rested)
