"""DB 接続の設定。SQLite と、差し替え先の DB で挙動を揃える。"""

from __future__ import annotations

from sqlalchemy.pool import NullPool

from app import db


def test_sqlite_gets_thread_safe_connections():
    kwargs = db._engine_kwargs("sqlite:///./data/app.db")
    assert kwargs["connect_args"]["check_same_thread"] is False


def test_in_memory_sqlite_does_not_need_a_directory(tmp_path):
    assert "connect_args" in db._engine_kwargs("sqlite://")


def test_other_databases_use_a_pool_by_default(monkeypatch):
    """常設サーバでは接続を使い回す。"""
    monkeypatch.setattr(db, "IS_SERVERLESS", False)
    kwargs = db._engine_kwargs("postgresql+psycopg://user:pw@host/db")
    assert kwargs == {"pool_pre_ping": True}


def test_serverless_does_not_pool_connections(monkeypatch):
    """Vercel では関数インスタンスが短命なので、接続を貯め込まない。

    プールを持つと、インスタンスが増えたときに DB 側の接続上限を食い潰す。
    """
    monkeypatch.setattr(db, "IS_SERVERLESS", True)
    kwargs = db._engine_kwargs("postgresql+psycopg://user:pw@host/db")
    assert kwargs == {"poolclass": NullPool}


def test_sqlite_path_failure_does_not_crash(monkeypatch):
    """読み取り専用のファイルシステムでも起動だけはできる。"""

    def boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(db.Path, "mkdir", boom)
    assert "connect_args" in db._engine_kwargs("sqlite:///./data/app.db")
