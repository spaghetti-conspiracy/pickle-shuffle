"""FastAPI アプリのエントリポイント。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.db import create_all
from app.errors import AppError

STATIC_DIR = Path(__file__).parent / "static"


class RevalidatingStaticFiles(StaticFiles):
    """毎回サーバに確認してから使わせる。

    既定では Cache-Control が付かず、ブラウザが独自の判断でキャッシュしてしまう。
    そうなると、修正を反映したのに端末側が古い JavaScript を使い続けることになる。
    ETag は付いているので、変わっていなければ 304 が返るだけで通信量は増えない。
    """

    def file_response(self, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


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

    @app.middleware("http")
    async def _do_not_cache_api(request: Request, call_next):  # noqa: ANN001, ANN202
        """API の応答はキャッシュさせない。

        表示画面は2秒ごとにポーリングしているので、古い応答を使われると
        練習会が消えたことにも気づけない。
        """
        response = await call_next(request)
        if request.url.path.startswith("/api"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(router)
    app.mount("/", RevalidatingStaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
