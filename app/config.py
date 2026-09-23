"""設定値。環境変数の読み取りはこのモジュールに集約する（CLAUDE.md 不変則6）。"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "sqlite:///./data/app.db"

IS_SERVERLESS = bool(os.environ.get("VERCEL"))
"""Vercel などのサーバーレス環境で動いているか。

関数インスタンスは短命で使い回されないため、接続プールを持たない方がよい。
"""


@dataclass(frozen=True)
class Settings:
    """アプリ全体の設定。"""

    database_url: str = DEFAULT_DATABASE_URL
    port: int = 8000
    public_base_url: str = ""
    """メンバーがアクセスできる URL。QR コードに埋め込む。

    既定ではブラウザが実際に叩いたホストを使うが、サーバと同じ PC で
    ``localhost`` として開いている場合、それをそのまま QR にすると
    スマートフォンから届かない。そういうときにここで上書きする。
    例: ``http://192.168.1.10:8000``
    """


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_settings() -> Settings:
    """環境変数から設定を読み込む。"""
    return Settings(
        database_url=os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL,
        port=_env_int("PORT", 8000),
        public_base_url=(os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/"),
    )


settings = load_settings()
