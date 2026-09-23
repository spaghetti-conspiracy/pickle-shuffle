"""設定値。環境変数の読み取りはこのモジュールに集約する（CLAUDE.md 不変則6）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.scheduler.domain import Weights

DEFAULT_DATABASE_URL = "sqlite:///./data/app.db"
DEFAULT_TENNISBEAR_URL = "https://www.tennisbear.net"

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
    tennisbear_base_url: str = DEFAULT_TENNISBEAR_URL
    """参加者の取り込み元。差し替えられるようにしておく（テストと、URL 変更に備えて）。"""

    tennisbear_timeout: float = 10.0
    """取り込みの待ち時間。待たせすぎるより、やり直してもらう方がよい。"""

    weights: Weights = field(default_factory=Weights)
    # 公平性の枠をどこまで緩めてよいか（試合数の差の上限）。既定の 0 は厳密公平。
    fairness_slack: int = 0
    # 何ラウンド先まで読むか。0 なら貪欲法。
    lookahead: int = 1
    # 先読みで比べる上位候補の数。
    beam: int = 16
    # 1回の生成で評価する候補集合の上限。超える分はサンプリングする。
    max_candidate_sets: int = 60


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
        tennisbear_base_url=(
            os.environ.get("TENNISBEAR_BASE_URL") or DEFAULT_TENNISBEAR_URL
        ).strip().rstrip("/"),
        tennisbear_timeout=_env_int("TENNISBEAR_TIMEOUT", 10),
        fairness_slack=_env_int("FAIRNESS_SLACK", 0),
        lookahead=_env_int("LOOKAHEAD", 1),
        beam=_env_int("BEAM", 16),
        max_candidate_sets=_env_int("MAX_CANDIDATE_SETS", 60),
    )


settings = load_settings()
