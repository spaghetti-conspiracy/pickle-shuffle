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
from app.db import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402


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
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine):
    """テスト用のセッションファクトリ。"""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def db(session_factory) -> Session:
    """DB セッション。"""
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(session_factory):
    """API のテストクライアント。"""
    application = create_app()

    def _override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_db] = _override_get_db
    with TestClient(application) as test_client:
        yield test_client
