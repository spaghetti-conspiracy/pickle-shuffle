"""DB を使える状態にするコマンド。

``python -m app.init_db``

**サーバーレスでは、これを1度だけ実行する。** 関数は冷えるたびに起動し直すので、
そのたびにテーブルの照合と合言葉のハッシュ計算をやると、1回目のアクセスが
何秒も遅くなる。そのため起動時の用意は既定で行わない（`SKIP_DB_INIT`）。

やること:

1. 足りないテーブルを作る（既存のテーブルには触らない）
2. 既定の団体と管理者を用意する（`ADMIN_PASSWORD` からハッシュを作る）

何度実行してもよい。列を変えたときは、これではなく移行スクリプトを使う。
"""

from __future__ import annotations

from app.config import settings
from app.db import SessionLocal, create_all
from app.services.owners import ensure_bootstrap


def main() -> None:
    create_all()
    with SessionLocal() as db:
        owner = ensure_bootstrap(db)
    print(f"用意しました: {settings.database_url}")
    print(f"  団体: {owner.name}")
    print("  管理者: 環境変数 ADMIN_PASSWORD の合言葉で入れます")


if __name__ == "__main__":
    main()
