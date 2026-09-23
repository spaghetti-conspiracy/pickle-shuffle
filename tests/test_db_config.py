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


def test_serverless_reuses_one_connection(monkeypatch):
    """Vercel では**接続を使い回す**。ただし1インスタンスにつき1本だけ。

    以前は「関数インスタンスは短命だから貯め込まない」として `NullPool` に
    していたが、実際には温まっている間くり返し使われる。毎回 TLS から
    張り直すと、遠い DB では**それだけで2秒**かかっていた（実測）。
    **ユーザー承認済みの方針変更。**

    一方で貯め込みもしない。関数は同時に何十個も立ち上がるので、
    1本ずつでも DB 側の上限には届く。
    """
    monkeypatch.setattr(db, "IS_SERVERLESS", True)
    kwargs = db._engine_kwargs("postgresql+psycopg://user:pw@host/db")
    assert kwargs["poolclass"] is not NullPool, "毎回張り直す設定に戻っている"
    assert kwargs["pool_size"] == 1
    assert kwargs["max_overflow"] == 0, "貯め込まない"
    assert kwargs["pool_pre_ping"] is True, "切れた接続を掴んだまま使わない"


def test_sqlite_path_failure_does_not_crash(monkeypatch):
    """読み取り専用のファイルシステムでも起動だけはできる。"""

    def boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(db.Path, "mkdir", boom)
    assert "connect_args" in db._engine_kwargs("sqlite:///./data/app.db")
