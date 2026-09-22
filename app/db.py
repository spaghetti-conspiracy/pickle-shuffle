"""DB 接続まわり。ORM のみを使い、DB 固有機能には依存しない（CLAUDE.md 不変則4）。"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import IS_SERVERLESS, settings


class Base(DeclarativeBase):
    """全 ORM モデルの基底クラス。"""


def _engine_kwargs(url: str) -> dict:
    """URL に応じた create_engine の追加引数を組み立てる。"""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        if IS_SERVERLESS:
            # 関数インスタンスは短命で、プールを持っても次のリクエストには
            # 引き継がれない。接続を貯め込んで DB 側の上限を食い潰さないようにする。
            return {"poolclass": NullPool}
        return {"pool_pre_ping": True}

    # SQLite のファイル DB は置き場所を作っておく。
    # サーバーレスではファイルシステムが読み取り専用なので、失敗しても止めない。
    if parsed.database and parsed.database != ":memory:":
        with contextlib.suppress(OSError):
            Path(parsed.database).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )

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
    """テーブルを作成する。記録は使い捨てのためマイグレーションは持たない。

    サーバーレスでは複数のインスタンスが同時に起動して、同じテーブルを
    作ろうとすることがある。実際にテーブルが揃っていれば競合は無視してよい。
    """
    from app import models  # noqa: F401  # モデル定義を Base に登録するため

    try:
        Base.metadata.create_all(bind=engine, checkfirst=True)
    except SQLAlchemyError:
        missing = set(Base.metadata.tables) - set(inspect(engine).get_table_names())
        if missing:
            raise


def get_db() -> Iterator[Session]:
    """FastAPI の依存性。リクエストごとにセッションを開いて閉じる。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
