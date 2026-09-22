"""アプリ共通の例外。API 層で HTTP ステータスへ変換する。"""

from __future__ import annotations


class AppError(Exception):
    """アプリ固有のエラーの基底クラス。"""

    status_code: int = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AppError):
    """対象が存在しない。"""

    status_code = 404


class ConflictError(AppError):
    """状態が競合していて操作できない（他端末が先に操作した場合など）。"""

    status_code = 409


class ValidationError(AppError):
    """入力値が不正。"""

    status_code = 422


class NotEnoughPlayersError(AppError):
    """出場可能なメンバーが足りず、マッチを組めない。"""

    status_code = 409
