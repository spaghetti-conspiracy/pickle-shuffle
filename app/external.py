"""取り込み元を含む識別子。

いまは tennisbear しか無いが、別のプラットフォームから取り込む日に備えて、
**どこから来た人か**を識別子そのものに持たせる。``bear:1614380`` の形。

列を「取り込み元」と「向こうの ID」に分けない。一意キーが
``(owner_id, external_id)`` の2列で済み、プラットフォームが増えても
テーブルを変えずに載るため。このアプリは「bear の人だけ一覧する」ような
検索をしないので、分ける利点が無い。
"""

from __future__ import annotations

import re

from app.errors import ValidationError

SOURCE_TENNISBEAR = "bear"
"""tennisbear.net の接頭辞。文字列リテラルをコード中に散らさない。"""

SOURCE_LABELS = {SOURCE_TENNISBEAR: "tennisbear"}
"""画面に出す名前。ID そのものは出さない（doc/spec.md）。"""

EXTERNAL_ID_MAX = 81
"""external_id の列の長さ。接頭辞16 + コロン + ID64。"""

_KEY = re.compile(r"^[a-z0-9]{1,16}:[A-Za-z0-9_-]{1,64}$")


def external_key(source: str, raw_id: str | int) -> str:
    """取り込み元と向こうの ID から識別子を組み立てる。

    >>> external_key(SOURCE_TENNISBEAR, 9001)
    'bear:9001'
    """
    key = f"{source}:{raw_id}"
    if not _KEY.fullmatch(key):
        raise ValidationError("取り込み元の識別子が不正です")
    return key


def split_external_key(key: str) -> tuple[str, str]:
    """識別子を取り込み元と向こうの ID に分ける。"""
    if not _KEY.fullmatch(key):
        raise ValidationError("取り込み元の識別子が不正です")
    source, _, raw_id = key.partition(":")
    return source, raw_id


def source_of(key: str | None) -> str | None:
    """識別子の取り込み元。手で登録した人は None。

    一覧で「取り込み／手登録」を見分けるために使う。
    """
    if key is None:
        return None
    return split_external_key(key)[0]


def raw_id_of(key: str | None) -> str | None:
    """識別子のうち、向こうの ID の部分。画面に出すのはイベント ID だけ。"""
    if key is None:
        return None
    return split_external_key(key)[1]
