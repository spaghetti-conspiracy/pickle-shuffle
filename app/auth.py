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
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), raw_salt, int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(_b64(expected), digest)


def issue_cookie(admin_id: int, password_hash: str) -> str:
    """クッキーの値を作る。

    **サーバ側に状態を持たない。** 再起動やサーバーレスの別インスタンスでも
    そのまま通り、パスワードを変えれば（ハッシュが変わるので）古いクッキーは
    自動的に無効になる。
    """
    return f"{admin_id}:{_sign(admin_id, password_hash)}"


def read_cookie(value: str | None) -> int | None:
    """クッキーから管理者の id を取り出す。署名はまだ見ない。

    誰の行を読めばよいかが分からないと照合できないので、2段階になる。
    """
    if not value:
        return None
    admin_id, _, _ = value.partition(":")
    try:
        return int(admin_id)
    except ValueError:
        return None


def cookie_matches(value: str, admin_id: int, password_hash: str) -> bool:
    """クッキーがその管理者のものか。"""
    return hmac.compare_digest(value, issue_cookie(admin_id, password_hash))


def _sign(admin_id: int, password_hash: str) -> str:
    return hmac.new(
        password_hash.encode(), str(admin_id).encode(), hashlib.sha256
    ).hexdigest()
