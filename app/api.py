"""HTTP API。ドメインの例外を HTTP ステータスへ変換する層。"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict[str, str]:
    """死活監視用。"""
    return {"status": "ok"}
