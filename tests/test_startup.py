"""起動まわりの設定。サーバーレスで遅くならないようにするための取り決め。"""

from __future__ import annotations

import os

from sqlalchemy.pool import NullPool, QueuePool

from app.config import load_settings


def _engine_kwargs(url: str, *, serverless: bool) -> dict:
    """`app.db._engine_kwargs` を、環境を差し替えて呼ぶ。"""
    import app.config
    import app.db

    before = app.db.IS_SERVERLESS
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
    assert kwargs["pool_size"] == 1, "定常で持ち続けるのは1本"
    # あふれ分まで 0 にすると、同時に来たリクエストが1本に直列化して
    # `pool_timeout` を超える。返却時に閉じるので貯め込みにはならない。
    assert 0 < kwargs["max_overflow"] <= 4, "同時に来たぶんを捌けず、かつ貯め込まない"


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


def test_the_db_is_prepared_automatically(monkeypatch):
    """**再起動すれば勝手に整う。**

    手で流し忘れると、デプロイは成功して最初の利用者が 500 を踏む。
    起動が多少遅れても、自動で確かめるほうがよい（**ユーザー判断**）。
    """
    monkeypatch.delenv("SKIP_DB_INIT", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    assert load_settings().skip_db_init is False, "確認を省いている"

    # サーバーレスでも省かない。`VERCEL` が実際に環境を切り替える変数。
    monkeypatch.setenv("VERCEL", "1")
    assert load_settings().skip_db_init is False, "サーバーレスだと確認を省いている"


def _count_queries(db, work) -> list[str]:
    """`work()` が実際に DB へ投げた SQL を数える。"""
    from sqlalchemy import event

    queries: list[str] = []
    engine = db.get_bind()

    def record(conn, cursor, statement, *args):  # noqa: ANN001
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        work()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return queries


def test_the_check_does_not_scale_with_the_number_of_tables(db):
    """確認の往復数が、テーブル数によらず一定であること。

    `create_all()` はテーブルを1つずつ照合するので、テーブルが増えるほど
    起動が延びる。遠い DB ではそれだけで数秒かかっていた。
    ここは **一覧を1回もらって照合する＋管理者を1回聞く** の2往復で固定する。
    """
    from app.db import Base, needs_setup

    queries = _count_queries(db, lambda: needs_setup(db))
    assert len(queries) == 2, f"往復が多い: {queries}"
    assert len(Base.metadata.tables) > 2, "テーブルが少なすぎて、この性質を確かめられない"
    assert any("admins" in q.lower() for q in queries), "管理者を見ていない"


def test_it_notices_when_a_table_is_missing(db):
    """**テーブルが足りなければ、用意が要ると答えること。**

    モデルにテーブルを1つ足して配ったとき、ここで気づけないと
    そのテーブルは永久に作られず、触った瞬間に 500 になる。
    「管理者がいるか」だけを見ていると、この状態を見逃す。
    """
    from sqlalchemy import inspect, text

    from app.db import Base, needs_setup

    assert needs_setup(db) is False, "用意済みなのに要ると言っている"

    engine = db.get_bind()
    victim = "round_participation"
    assert victim in Base.metadata.tables, "テスト対象のテーブル名が変わっている"
    db.rollback()
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE {victim}"))
    assert victim not in inspect(engine).get_table_names(), "消せていない"

    assert needs_setup(db) is True, "テーブルが欠けているのに要らないと言っている"


def test_it_notices_when_the_db_is_not_ready(db):
    """用意できていなければ、そう答えること。"""
    from sqlalchemy import delete

    from app.db import needs_setup
    from app.models import Admin

    assert needs_setup(db) is False, "用意済みなのに要ると言っている"
    db.execute(delete(Admin))
    db.commit()
    assert needs_setup(db) is True, "管理者がいないのに要らないと言っている"


def test_the_setting_can_be_forced_either_way(monkeypatch):
    """環境変数で明示的に上書きできること。"""
    monkeypatch.delenv("VERCEL", raising=False)
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("SKIP_DB_INIT", value)
        assert load_settings().skip_db_init is True, f"{value} で立たない"

    monkeypatch.setenv("VERCEL", "1")
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


def _run_lifespan(monkeypatch, *, needs: bool, boom: bool = False) -> dict:
    """`app.main.lifespan` を、DB を触らずに走らせて何を呼んだか見る。"""
    import asyncio
    import contextlib
    import dataclasses

    from sqlalchemy.exc import OperationalError

    import app.main

    calls: dict = {"create_all": 0, "ensure_bootstrap": 0}

    @contextlib.contextmanager
    def fake_session():
        yield object()

    def fake_needs_setup(db):  # noqa: ANN001
        if boom:
            raise OperationalError("select 1", {}, Exception("DB が落ちている"))
        return needs

    monkeypatch.setattr(app.main, "SessionLocal", fake_session)
    monkeypatch.setattr(app.main, "needs_setup", fake_needs_setup)
    monkeypatch.setattr(app.main, "settings", dataclasses.replace(app.main.settings, skip_db_init=False))
    monkeypatch.setattr(app.main, "create_all", lambda: calls.__setitem__("create_all", calls["create_all"] + 1))
    monkeypatch.setattr(
        app.main,
        "ensure_bootstrap",
        lambda db: calls.__setitem__("ensure_bootstrap", calls["ensure_bootstrap"] + 1),
    )

    async def go():
        async with app.main.lifespan(None):
            pass

    asyncio.run(go())
    return calls


def test_startup_prepares_the_db_when_it_is_not_ready(monkeypatch):
    """**足りなければ、起動時にそのとき作る。**

    再起動すれば勝手に整うほうが、手で流し忘れて最初の利用者が
    500 を踏むより良い（ユーザー判断）。
    """
    calls = _run_lifespan(monkeypatch, needs=True)
    assert calls["create_all"] == 1, "テーブルを作っていない"
    assert calls["ensure_bootstrap"] == 1, "団体と管理者を用意していない"


def test_startup_does_nothing_when_the_db_is_ready(monkeypatch):
    """用意できていれば、起動を1往復以上遅らせないこと。"""
    calls = _run_lifespan(monkeypatch, needs=False)
    assert calls == {"create_all": 0, "ensure_bootstrap": 0}, "毎回フル点検に戻っている"


def test_startup_survives_a_db_outage(monkeypatch):
    """**用意に失敗しても、アプリは立ち上がること。**

    ここで例外を投げるとアプリ全体が起動せず、静的ファイルもメンバー用
    画面も含めて全部 500 になる。DB が一時的に落ちているだけなら、
    次の起動で整えばよい。
    """
    calls = _run_lifespan(monkeypatch, needs=True, boom=True)
    assert calls["create_all"] == 0, "落ちている DB に作りにいっている"
