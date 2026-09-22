"""Vercel のサーバーレス関数のエントリポイント。

Vercel の Python ランタイムは、このファイルの ``app`` を ASGI アプリとして扱う。
ローカルやコンテナでは使わない（`app.main:app` を直接起動する）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 関数の作業ディレクトリからでも app パッケージを読めるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

__all__ = ["app"]
