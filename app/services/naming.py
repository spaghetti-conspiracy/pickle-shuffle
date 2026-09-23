"""ニックネームの重複を避ける。

台帳の中でも、練習会の参加者リストの中でも、同じ規則で番号を振る。
どちらからも使うので、独立した小さなモジュールに置く。
"""

from __future__ import annotations

from app.models import NICKNAME_MAX


def unique_nickname(base: str, taken: set[str]) -> str:
    """重複しないニックネームにする。先にいる人はそのまま、後の人に番号を振る。

    取り込み元の ID で区別はできるが、画面に出すには細かすぎる。
    「マッツ」「マッツ2」なら読み上げにも使える。番号はその一覧の中でだけ
    意味を持つ（不変則14: ニックネームは識別子ではない）。
    """
    base = base[:NICKNAME_MAX]
    if base not in taken:
        return base
    number = 2
    while True:
        suffix = str(number)
        candidate = base[: NICKNAME_MAX - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        number += 1
