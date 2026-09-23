"""DB 接続まわり。ORM のみを使い、DB 固有機能には依存しない（CLAUDE.md 不変則4）。"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, make_url, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config import IS_SERVERLESS, settings


class Base(DeclarativeBase):
    """全 ORM モデルの基底クラス。"""


def _engine_kwargs(url: str) -> dict:
    """URL に応じた create_engine の追加引数を組み立てる。"""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        if IS_SERVERLESS:
            # **接続は使い回す。** 以前は「関数インスタンスは短命だから」と
            # `NullPool` にしていたが、実際には温まっている間くり返し使われる。
            # 毎回 TLS から張り直すと、遠い DB（Neon はアジアだとシンガポール
            # しかない）では**それだけで2秒**かかっていた。
            #
            # 定常では1インスタンスにつき1本だけ持つ。関数は同時に何十個も
            # 立ち上がるので、1本ずつでも DB 側の上限には届く。貯め込まない。
            #
            # ただし**上限を1本にはしない**。API は全部同期の `def` なので
            # Starlette はスレッドプールで並行に捌く。つまり1インスタンスが
            # 同時に複数のリクエストを持つ。1本に直列化すると、終盤に端末が
            # 揃って聞きに来たときに `pool_timeout` を超えて 500 になる。
            # あふれた分は返却時に閉じるので、貯め込みにはならない。
            return {
                "poolclass": QueuePool,
                "pool_size": 1,
                "max_overflow": 3,
                # 寝かせたままの接続は相手に切られていることがある。
                # 使う前に1往復で確かめる（張り直すよりはるかに安い）。
                "pool_pre_ping": True,
                # Neon の無料枠はアイドルで止まる。長く持ち続けない。
                "pool_recycle": 300,
                # 空くのを待つより、待たせすぎない方がよい。
                "pool_timeout": 10,
            }
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
    def _configure_sqlite(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        """SQLite の挙動を、差し替え先の DB に近づける。

        * 外部キー制約 — SQLite は既定で検査しない。有効にしておかないと、
          他の DB に差し替えたときだけ参照整合性の不具合が出ることになる。
        * WAL — 既定のジャーナルでは読んでいる間は書けない。管理画面と表示画面が
          同時にアクセスするので、読み書きを並行できるようにする。
        * busy_timeout — 書き込みが競合したとき、即座に諦めず少し待つ。
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
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


def needs_setup(db: Session) -> bool:
    """用意が要るかを、**2往復だけで**見る。

    起動のたびに `create_all()` を呼ぶと、テーブルを1つずつ照合するために
    DB へ何度も往復する。遠い DB では、それだけで数秒かかっていた。

    代わりに、次の2つだけを聞く。

    1. **テーブルが揃っているか**（一覧を1回もらって照合する）。
       モデルにテーブルを足して配ったとき、ここで気づけないと
       そのテーブルは永久に作られず、触った瞬間に 500 になる。
       不変則8（`create_all` で用意する）はこの確認に支えられている。
    2. **管理者が1人でもいるか**。テーブルはあるが中身が空、という
       作りかけの状態を拾う。
    """
    from app.models import Admin  # 循環 import を避けるため、ここで読む

    try:
        existing = set(inspect(db.get_bind()).get_table_names())
        if set(Base.metadata.tables) - existing:
            return True
        return db.execute(select(Admin.id).limit(1)).first() is None
    except SQLAlchemyError:
        db.rollback()
        return True


def get_db() -> Iterator[Session]:
    """FastAPI の依存性。リクエストごとにセッションを開いて閉じる。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
