"""メンバー台帳。

練習会には属さない「人」の一覧。追加・修正・削除はここ。
**保持するのは属性だけで、統計は絶対に共有しない**（不変則13）。
統計のスコープを決めるのは ``members.session_id`` である、という構造は変えない。

台帳と練習会の参加者の関係:

- 台帳を直したら、その人が入っている練習会の参加者にも書き写す（下り）。
  マッチの組み方に効くのは次の生成からで、生成済みのカードは動かない（不変則12）
- 台帳から人を消しても、参加者行と過去の記録は残る。切れるのは ``person_id`` だけ。
  進行中の練習会が壊れないようにするため
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import NotFoundError, ValidationError
from app.external import external_key, source_of
from app.models import NICKNAME_MAX, Member, Owner, Person
from app.scheduler.domain import Gender, Level, MemberStatus
from app.services.naming import unique_nickname


def list_people(db: Session, owner: Owner) -> list[Person]:
    """台帳の一覧。名前の順に出す。"""
    return list(
        db.scalars(
            select(Person)
            .where(Person.owner_id == owner.id)
            .order_by(Person.nickname, Person.id)
        )
    )


def get_person(db: Session, owner: Owner, person_id: int) -> Person:
    person = db.get(Person, person_id)
    if person is None or person.owner_id != owner.id:
        raise NotFoundError("メンバーが見つかりません")
    return person


def find_by_external(db: Session, owner: Owner, external_id: str) -> Person | None:
    """取り込み元の識別子で引く。

    ニックネームでは引かない。同名の別人の属性をそのまま被ってしまう
    （「マッツ」を初心者に直したら、別の「マッツ」も初心者で入る）。
    """
    return db.scalars(
        select(Person).where(
            Person.owner_id == owner.id, Person.external_id == external_id
        )
    ).first()


def taken_nicknames(db: Session, owner: Owner, *, exclude_id: int | None = None) -> set[str]:
    """台帳で使われている名前。番号を振るために使う。"""
    return {
        person.nickname
        for person in list_people(db, owner)
        if exclude_id is None or person.id != exclude_id
    }


def add_person(
    db: Session,
    owner: Owner,
    *,
    nickname: str,
    gender: Gender,
    level: Level,
    external_id: str | None = None,
    commit: bool = True,
) -> Person:
    """台帳に1人足す。

    同名は禁じない。先にいる人はそのまま、後の人に番号を振る（不変則14）。
    """
    nickname = _clean(nickname)
    person = Person(
        owner_id=owner.id,
        nickname=unique_nickname(nickname, taken_nicknames(db, owner)),
        gender=gender,
        level=level,
        external_id=external_id,
    )
    db.add(person)
    if not commit:
        db.flush()
        return person
    db.commit()
    db.refresh(person)
    return person


def update_person(
    db: Session,
    owner: Owner,
    person: Person,
    *,
    nickname: str | None = None,
    gender: Gender | None = None,
    level: Level | None = None,
    commit: bool = True,
) -> Person:
    """台帳の1人を直し、その人が入っている練習会にも反映する。

    直す場所を1つにするための下り方向の反映。生成済みのマッチは動かない
    （不変則12: 反映は次の生成から）。表示画面には ``updated_at`` を見て
    「登録情報が更新されました」が出る。
    """
    if nickname is not None:
        person.nickname = unique_nickname(
            _clean(nickname), taken_nicknames(db, owner, exclude_id=person.id)
        )
    if gender is not None:
        person.gender = gender
    if level is not None:
        person.level = level
    _propagate(db, person)
    if commit:
        db.commit()
        db.refresh(person)
    return person


def delete_person(db: Session, owner: Owner, person: Person) -> None:
    """台帳から消す。**練習会の参加者と過去の記録には触らない。**

    進行中の練習会からいきなり人が抜けると事故になる。切れるのは
    ``person_id`` だけで（``ondelete="SET NULL"``）、名前も属性も記録も残る。
    練習会から外すのは管理画面の「外す」で、別の操作。
    """
    db.delete(person)
    db.commit()


def sync_from_member(db: Session, member: Member) -> None:
    """練習会側で直した属性を台帳に上げる（上り）。

    **ニックネームは上げない。** 練習会の中での番号付けや読み上げ用の
    言い換えで、台帳の名前を書き換えてしまわないため。
    """
    if member.person_id is None:
        return
    person = db.get(Person, member.person_id)
    if person is None:
        return
    person.gender = member.gender
    person.level = member.level


def session_counts(db: Session, owner: Owner) -> dict[int, int]:
    """台帳の人ごとの「いま参加者として入っている練習会の数」。

    台帳から消しても進行中の練習会は壊れないが、消してよいかの目安として
    一覧に出す。離脱した行は数えない。
    """
    rows = db.execute(
        select(Member.person_id, func.count(func.distinct(Member.session_id)))
        .join(Person, Person.id == Member.person_id)
        .where(Person.owner_id == owner.id, Member.status != MemberStatus.LEFT)
        .group_by(Member.person_id)
    )
    return {person_id: count for person_id, count in rows if person_id is not None}


def numbered_pairs(people: list[Person]) -> set[int]:
    """番号で見分けている人たちの id。

    台帳は同名に番号を振るので、まったく同じ名前は並ばない。並ぶのは
    「渡辺ともみ」と「渡辺ともみ2」のような組で、**手で登録したあとに
    同じ人を取り込んでしまった**ときがまさにこの形になる。統合はしない
    方針なので、どちらを消すかを選べるように一覧で知らせる。

    「m1」と「m2」のように、たまたま数字で終わる別々の名前は組にしない。
    片方がもう片方＋数字になっている場合だけを見る。
    """
    flagged: set[int] = set()
    for person in people:
        for other in people:
            if other.id == person.id:
                continue
            longer, shorter = person.nickname, other.nickname
            if len(shorter) > len(longer):
                longer, shorter = shorter, longer
            tail = longer[len(shorter) :]
            if longer.startswith(shorter) and tail.isdigit():
                flagged.add(person.id)
                break
    return flagged


def source_label(person: Person) -> str | None:
    """どこから取り込んだ人か。手で登録した人は None。

    ID そのものは出さない（`doc/spec.md`）。一覧で二重登録を見分けるために使う。
    """
    return source_of(person.external_id)


def external_id_for(source: str, raw_id: str | int) -> str:
    """取り込み元の識別子を組み立てる。接頭辞を散らさないための入口。"""
    return external_key(source, raw_id)


def _clean(nickname: str) -> str:
    if not nickname.strip():
        raise ValidationError("ニックネームを入力してください")
    return nickname.strip()[:NICKNAME_MAX]


def _propagate(db: Session, person: Person) -> None:
    """台帳の値を、その人が入っている練習会の参加者に書き写す。"""
    members = list(
        db.scalars(select(Member).where(Member.person_id == person.id))
    )
    for member in members:
        if member.status is MemberStatus.LEFT:
            # 離脱した人の行は記録のためにあるので、もう書き換えない。
            continue
        member.gender = person.gender
        member.level = person.level
        taken = {
            other.nickname
            for other in db.scalars(
                select(Member).where(Member.session_id == member.session_id)
            )
            if other.id != member.id and other.status is not MemberStatus.LEFT
        }
        member.nickname = unique_nickname(person.nickname, taken)
