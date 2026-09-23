"""起動まわりの設定。サーバーレスで遅くならないようにするための取り決め。"""

from __future__ import annotations

import os

from sqlalchemy.pool import NullPool, QueuePool

from app.config import load_settings


def _engine_kwargs(url: str, *, serverless: bool) -> dict:
    """`app.db._engine_kwargs` を、環境を差し替えて呼ぶ。"""
    import app.config
    import app.db

    before = app.config.IS_SERVERLESS
    app.db.IS_SERVERLESS = serverless
    try:
        return app.db._engine_kwargs(url)
    finally:
        app.db.IS_SERVERLESS = before


POSTGRES = "postgresql+psycopg://u:p@example.invalid/db"


def test_serverless_reuses_one_connection():
    """**接続は使い回す。**

    毎回張り直すと、遠い DB では TLS のやり取りだけで秒が飛ぶ。
    実測で、1リクエストあたり 2 秒かかっていた。
    """
    kwargs = _engine_kwargs(POSTGRES, serverless=True)
    assert kwargs["poolclass"] is QueuePool, "NullPool に戻っている"
    assert kwargs["pool_size"] == 1, "1インスタンスにつき1本にする"
    assert kwargs["max_overflow"] == 0, "貯め込まない"


def test_serverless_checks_the_connection_before_using_it():
    """寝ている間に切られた接続を掴んだまま使わない。"""
    kwargs = _engine_kwargs(POSTGRES, serverless=True)
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] <= 600, "アイドルで止まる DB に長く持ち続けない"


def test_sqlite_is_not_pooled_that_way():
    """SQLite は別扱い。プールの設定を持ち込まない。"""
    kwargs = _engine_kwargs("sqlite:///./x.db", serverless=True)
    assert "poolclass" not in kwargs
    assert kwargs["connect_args"] == {"check_same_thread": False}
    assert NullPool is not None  # import の意図を明示（比較対象として残す）


def test_serverless_does_not_prepare_the_db_on_every_start(monkeypatch):
    """**サーバーレスでは既定で用意しない。**

    関数は冷えるたびに起動し直す。そのたびにテーブルの照合と pbkdf2 20万回が
    走ると、1回目のアクセスが何秒も遅くなる。用意は `python -m app.init_db` で
    1度だけ行う。
    """
    import app.config

    monkeypatch.delenv("SKIP_DB_INIT", raising=False)
    monkeypatch.setattr(app.config, "IS_SERVERLESS", True)
    assert load_settings().skip_db_init is True, "サーバーレスで毎回用意している"

    monkeypatch.setattr(app.config, "IS_SERVERLESS", False)
    assert load_settings().skip_db_init is False, "手元では今までどおり用意する"


def test_the_setting_can_be_forced_either_way(monkeypatch):
    """環境変数で明示的に上書きできること。"""
    import app.config

    monkeypatch.setattr(app.config, "IS_SERVERLESS", False)
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("SKIP_DB_INIT", value)
        assert load_settings().skip_db_init is True, f"{value} で立たない"

    monkeypatch.setattr(app.config, "IS_SERVERLESS", True)
    monkeypatch.setenv("SKIP_DB_INIT", "0")
    assert load_settings().skip_db_init is False, "明示的に切れない"


def test_one_command_prepares_everything(db):
    """`python -m app.init_db` だけで、テーブルも管理者も用意できること。

    サーバーレスでは起動時に用意しないので、この1本が抜けると
    「デプロイはできたのに、合言葉が通らない」になる。
    """
    from sqlalchemy import select

    from app.models import Admin, Owner
    from app.services.owners import ensure_bootstrap

    # `init_db.main()` がやることと同じ（テストは engine を差し替えているので、
    # ここではテーブル作成済みの db に対して用意の部分だけを確かめる）。
    owner = ensure_bootstrap(db)
    assert owner.id is not None
    assert db.scalars(select(Owner)).all(), "団体が用意されていない"
    assert db.scalars(select(Admin)).all(), "管理者が用意されていない"


def test_skipping_startup_still_serves(monkeypatch, session_factory):
    """用意を省いても、アプリは立ち上がって応答する。

    省くのは用意だけで、すでに用意された DB を使う分には変わらない。
    """
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import create_app

    monkeypatch.setenv("SKIP_DB_INIT", "1")
    application = create_app()

    def _override():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_db] = _override
    with TestClient(application) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/sessions").status_code == 401


def test_the_environment_variable_is_read_in_one_place():
    """`os.environ` を直接読まない（CLAUDE.md 不変則6）。"""
    import app.main

    with open(app.main.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "os.environ" not in source
    assert "settings.skip_db_init" in source
    assert os.environ is not None
