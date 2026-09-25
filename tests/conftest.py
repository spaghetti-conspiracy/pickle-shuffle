"""テスト共通のフィクスチャ。DB はインメモリ SQLite を使う。"""

from __future__ import annotations

import os

# app のモジュールを import する前に、テスト用の DB を指定しておく。
# setdefault にしない。シェルに DATABASE_URL が export されていると
# それが採用され、lifespan の create_all が本番 DB にテーブルを作ってしまう。
# テストが環境に左右されないことは利点であって不便ではない。
os.environ["DATABASE_URL"] = "sqlite://"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: E402,F401  # テーブル定義を Base に登録する
from app.config import settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.owners import current_owner, ensure_bootstrap  # noqa: E402


@pytest.fixture
def engine():
    """テストごとに独立したインメモリ DB。"""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    # 既定の団体と管理者。本番の起動時と同じものを用意しておく。
    with sessionmaker(bind=eng, future=True)() as boot:
        ensure_bootstrap(boot)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine):
    """テスト用のセッションファクトリ。"""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def owner(db):
    """既定の団体。サービス層は必ず団体を受け取る。"""
    return current_owner(db)


@pytest.fixture
def db(session_factory) -> Session:
    """DB セッション。"""
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def _test_app(session_factory):
    application = create_app()

    def _override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_db] = _override_get_db
    return application


@pytest.fixture
def guest_client(session_factory):
    """合言葉を通していないクライアント。門そのものを試すのに使う。"""
    with TestClient(_test_app(session_factory)) as test_client:
        yield test_client


@pytest.fixture
def client(session_factory):
    """API のテストクライアント。管理者として合言葉を通した状態。

    練習会の一覧・作成と台帳には門がかかっている。ふだんのテストは
    管理者が操作している場面を見たいので、ここで一度通しておく。
    """
    with TestClient(_test_app(session_factory)) as test_client:
        response = test_client.post("/api/login", json={"password": settings.admin_password})
        assert response.status_code == 204, response.text
        yield test_client
