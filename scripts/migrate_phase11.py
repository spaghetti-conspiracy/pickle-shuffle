"""Phase 11 の移行。メンバー台帳の分離と、団体・管理者の追加。

**サービスを止めてから、1度だけ実行する**（CLAUDE.md 不変則8）。
実行後は `docker compose up -d` で戻す。

やること:

1. 足りないテーブルを作る（`owners` / `admins` / `owner_admins` / `people`）
2. 既存の行を既定の団体に紐づける
3. `member_profiles` の属性を `people` に移す（`tennisbear_user_id` は
   `bear:<id>` の形にする）
4. `members` を `person_id` で台帳に繋ぐ。取り込み元の ID で引き当て、
   無ければ名前で引き当て、それも無ければ台帳に作る
5. `practice_sessions.tennisbear_event_id` を `external_event_id` に移す

**元に戻せない。** 実行前にバックアップを取ること:

    docker compose exec db pg_dump -U pickle pickle > backup.sql

使い方:

    DATABASE_URL=... .venv/bin/python scripts/migrate_phase11.py [--dry-run]
"""

from __future__ import annotations

import sys

from sqlalchemy import inspect, select, text

from app.db import SessionLocal, create_all, engine
from app.external import SOURCE_TENNISBEAR, external_key
from app.models import Member, Person, PracticeSession
from app.services.owners import ensure_bootstrap


def _columns(table: str) -> set[str]:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _add_column(table: str, definition: str, name: str) -> None:
    """列を足す。すでにあれば何もしない。

    `create_all` は既存のテーブルに列を足さないので、ここだけ DDL を書く。
    使い捨ての移行スクリプトなので、ORM を正とする方針（不変則4）の例外。
    """
    if name in _columns(table):
        print(f"  {table}.{name}: すでにある")
        return
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {definition}"))
    print(f"  {table}.{name}: 足した")


def migrate(dry_run: bool = False) -> None:
    before = _columns("members")
    if not before:
        print("members が無い。まっさらな DB なので移行は要らない。")
        create_all()
        with SessionLocal() as db:
            ensure_bootstrap(db)
        return

    print("1) 足りないテーブルを作る")
    create_all()

    print("2) 列を足す")
    _add_column("practice_sessions", "owner_id INTEGER", "owner_id")
    _add_column("practice_sessions", "external_event_id VARCHAR(81)", "external_event_id")
    _add_column("members", "person_id INTEGER", "person_id")
    _add_column("members", "joined_by_import BOOLEAN DEFAULT FALSE", "joined_by_import")

    if dry_run:
        print("--dry-run なのでここまで。行は触っていない。")
        return

    with SessionLocal() as db:
        print("3) 既定の団体と管理者を用意する")
        owner = ensure_bootstrap(db)

        print("4) 練習会を団体に紐づける")
        db.execute(
            text("UPDATE practice_sessions SET owner_id = :owner WHERE owner_id IS NULL"),
            {"owner": owner.id},
        )
        if "tennisbear_event_id" in _columns("practice_sessions"):
            for session in db.scalars(select(PracticeSession)):
                old = db.execute(
                    text("SELECT tennisbear_event_id FROM practice_sessions WHERE id = :id"),
                    {"id": session.id},
                ).scalar()
                if old is not None and session.external_event_id is None:
                    session.external_event_id = external_key(SOURCE_TENNISBEAR, old)

        print("5) 台帳を作る")
        moved = 0
        if "member_profiles" in inspect(engine).get_table_names():
            rows = db.execute(text("SELECT nickname, gender, level, tennisbear_user_id FROM member_profiles")).all()
            for nickname, gender, level, user_id in rows:
                key = external_key(SOURCE_TENNISBEAR, user_id) if user_id else None
                db.add(
                    Person(
                        owner_id=owner.id,
                        nickname=nickname,
                        gender=gender,
                        level=level,
                        external_id=key,
                    )
                )
                moved += 1
            db.flush()
        print(f"  {moved} 人を台帳に移した")

        print("6) 参加者を台帳に繋ぐ")
        people = list(db.scalars(select(Person).where(Person.owner_id == owner.id)))
        by_external = {p.external_id: p for p in people if p.external_id}
        by_name: dict[str, Person] = {}
        for person in people:
            by_name.setdefault(person.nickname, person)

        had_tennisbear = "tennisbear_user_id" in _columns("members")
        linked = created = 0
        for member in db.scalars(select(Member)):
            if member.person_id is not None:
                continue
            person = None
            if had_tennisbear:
                user_id = db.execute(
                    text("SELECT tennisbear_user_id FROM members WHERE id = :id"),
                    {"id": member.id},
                ).scalar()
                if user_id:
                    person = by_external.get(external_key(SOURCE_TENNISBEAR, user_id))
            if person is None:
                person = by_name.get(member.nickname)
            if person is None:
                person = Person(
                    owner_id=owner.id,
                    nickname=member.nickname,
                    gender=member.gender,
                    level=member.level,
                )
                db.add(person)
                db.flush()
                by_name.setdefault(person.nickname, person)
                created += 1
            member.person_id = person.id
            member.joined_by_import = bool(person.external_id)
            linked += 1
        print(f"  {linked} 人を繋いだ（うち {created} 人は台帳に新しく作った）")

        db.commit()

    print("\n終わり。古い列（tennisbear_* / member_profiles）は残してある。")
    print("動作を確かめてから、落ち着いて消すこと。")


if __name__ == "__main__":
    migrate(dry_run="--dry-run" in sys.argv)
