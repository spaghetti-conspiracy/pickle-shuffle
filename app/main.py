"""FastAPI アプリのエントリポイント。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from app.api import public_router, router
from app.config import settings
from app.db import SessionLocal, create_all
from app.errors import AppError
from app.services.owners import ensure_bootstrap

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
    """起動時にテーブルを作り、既定の団体と管理者を用意する。

    **サーバーレスでは毎回はやらない。** 関数は冷えるたびに起動し直すので、
    そのたびにテーブルの照合（`create_all`）と合言葉のハッシュ計算
    （pbkdf2 を20万回）をやると、冷えた1回目が10秒近くかかる。
    手元の Postgres 相手でも 0.22 秒、遠い DB ならその何倍にもなる。

    サーバーレスでは `SKIP_DB_INIT=1` を立てて、用意は
    `python -m app.init_db` で1度だけ行う（DEPLOY_SETUP.md）。
    """
    if settings.skip_db_init:
        yield
        return
    create_all()
    with SessionLocal() as db:
        ensure_bootstrap(db)
    yield


def create_app() -> FastAPI:
    """アプリを組み立てる。テストからも使う。"""
    app = FastAPI(title="ピックルボール対戦カード生成", lifespan=lifespan)

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "code": exc.code},
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        """一意制約に触れたら 409 にする。

        別の端末が先に同じ操作を終えていた、という場面でしか起きない。
        500 にすると表示画面が「通信できません」を出して止まってしまう。
        """
        return JSONResponse(
            status_code=409,
            content={"detail": "ほかの端末が先に操作しました", "code": "conflict"},
        )

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

    app.include_router(public_router)
    app.include_router(router)
    app.mount("/", RevalidatingStaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
