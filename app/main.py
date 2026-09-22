"""FastAPI アプリのエントリポイント。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.db import create_all
from app.errors import AppError

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時にテーブルを作成する。"""
    create_all()
    yield


def create_app() -> FastAPI:
    """アプリを組み立てる。テストからも使う。"""
    app = FastAPI(title="ピックルボール対戦カード生成", lifespan=lifespan)

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    app.include_router(router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
