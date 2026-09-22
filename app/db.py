"""DB 接続まわり。ORM のみを使い、DB 固有機能には依存しない（CLAUDE.md 不変則4）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """全 ORM モデルの基底クラス。"""


def _engine_kwargs(url: str) -> dict:
    """URL に応じた create_engine の追加引数を組み立てる。"""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return {}

    # SQLite のファイル DB は置き場所を作っておく。
    if parsed.database and parsed.database != ":memory:":
        Path(parsed.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    # uvicorn のワーカースレッドから触るため。
    return {"connect_args": {"check_same_thread": False}}


engine = create_engine(settings.database_url, future=True, **_engine_kwargs(settings.database_url))


if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        """SQLite の外部キー制約を有効にする。

        SQLite は既定で FK を検査しないため、他の DB に差し替えたときだけ
        参照整合性の不具合が出る、という事態になりかねない。挙動を揃えておく。
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def create_all() -> None:
    """テーブルを作成する。記録は使い捨てのためマイグレーションは持たない。"""
    from app import models  # noqa: F401  # モデル定義を Base に登録するため

    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI の依存性。リクエストごとにセッションを開いて閉じる。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
