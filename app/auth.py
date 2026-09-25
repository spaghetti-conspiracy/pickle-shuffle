"""管理者の合言葉。

**いたずら防止であって、秘密を守る仕組みではない。** 個人の PC や
LAN の中で使う前提で、知らない人に練習会を作られたり台帳を覗かれたり
しないようにするだけ。

管理者の登録画面は作らない。環境変数 ``ADMIN_PASSWORD`` から固定の1人を
起動時に用意し、その1人のパスワードだけを見る。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

COOKIE_NAME = "pickle_admin"
"""合言葉を通したことを覚えておくクッキー。"""

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 200_000
"""標準ライブラリだけで済ませる（新しい依存を入れない）。"""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """``pbkdf2_sha256$<回数>$<salt>$<hash>`` の形にして返す。"""
    salt = secrets.token_bytes(16) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """保存したハッシュと突き合わせる。比較は時間を一定にする。"""
    try:
        algorithm, iterations, salt, digest = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        raw_salt = base64.urlsafe_b64decode(salt + "=" * (-len(salt) % 4))
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), raw_salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(_b64(expected), digest)


def issue_cookie(admin_id: int, password_hash: str) -> str:
    """クッキーの値を作る。

    **サーバ側に状態を持たない。** 再起動やサーバーレスの別インスタンスでも
    そのまま通る。

    パスワードを変えると、DB のハッシュが作り直された時点で古いクッキーは
    無効になる。**作り直しは即座ではない**（起動時の用意か、次にログインが
    1回失敗したとき）。合言葉を変えた本人が新しい合言葉で入り直せば、
    そのログインが作り直しを起こすので、そこで古いクッキーは切れる。
    """
    return f"{admin_id}:{_sign(admin_id, password_hash)}"


MAX_COOKIE_LENGTH = 128
"""受け取るクッキーの長さの上限。これを超える値は見るまでもなく捨てる。"""

MAX_ADMIN_ID = 2**63 - 1
"""id の上限。DB の整数に収まらない値を渡されて落ちないようにする。"""


def read_cookie(value: str | None) -> int | None:
    """クッキーから管理者の id を取り出す。署名はまだ見ない。

    誰の行を読めばよいかが分からないと照合できないので、2段階になる。

    **細工された値で落ちないこと。** 合言葉を持たない相手が自由に送れる入口
    なので、桁あふれや非 ASCII で 500 を返すようでは門の意味が薄れる。
    """
    if not value or len(value) > MAX_COOKIE_LENGTH:
        return None
    admin_id, separator, signature = value.partition(":")
    if not separator or not admin_id.isdecimal() or not signature.isascii():
        return None
    number = int(admin_id)
    return number if 0 < number <= MAX_ADMIN_ID else None


def cookie_matches(value: str, admin_id: int, password_hash: str) -> bool:
    """クッキーがその管理者のものか。

    比較は bytes で行う。`compare_digest` は非 ASCII の str を渡すと
    例外になるので、細工された値で 500 にしないため。
    """
    expected = issue_cookie(admin_id, password_hash)
    return hmac.compare_digest(value.encode("utf-8", "replace"), expected.encode())


def _sign(admin_id: int, password_hash: str) -> str:
    return hmac.new(password_hash.encode(), str(admin_id).encode(), hashlib.sha256).hexdigest()
