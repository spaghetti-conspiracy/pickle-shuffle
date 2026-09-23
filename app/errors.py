"""アプリ共通の例外。API 層で HTTP ステータスへ変換する。"""

from __future__ import annotations


class AppError(Exception):
    """アプリ固有のエラーの基底クラス。"""

    status_code: int = 400
    code: str = "error"
    """画面側が種別で分岐するための識別子。

    HTTP のステータスだけだと足りない。たとえば 409 には「他端末が先に
    操作した（黙って追従してよい）」と「人数が足りない（利用者に伝える
    べき）」が混ざっていて、文言で判定するわけにはいかない。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AppError):
    """対象が存在しない。"""

    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    """状態が競合していて操作できない（他端末が先に操作した場合など）。"""

    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    """入力値が不正。"""

    status_code = 422
    code = "invalid"


class NotEnoughPlayersError(AppError):
    """出場可能なメンバーが足りず、マッチを組めない。"""

    status_code = 409
    code = "not_enough_players"
