"""テーブルを作るだけのコマンド。

``python -m app.init_db``

Vercel など、起動のたびに作成させたくない環境で1度だけ実行する用。
"""

from __future__ import annotations

from app.config import settings
from app.db import create_all


def main() -> None:
    create_all()
    print(f"テーブルを作成しました: {settings.database_url}")


if __name__ == "__main__":
    main()
