# ピックルボール対戦カード生成アプリ

練習会の対戦カードを自動で組むウェブアプリ。管理画面でメンバーを登録し、
試合表示画面をタブレットに出しっぱなしにして使う。

- 仕様: `doc/spec.md`
- アルゴリズムの説明: `doc/algorithm.md`
- 開発手順と進捗: `PLAN.md`
- 開発規則: `CLAUDE.md`

## 動かす

### 手元の PC で動かす（既定）

```bash
docker compose up --build
```

`http://localhost:8000` を開く。タブレットからは、同じ LAN にいる PC の IP を使って
`http://<PC の IP>:8000` を開く。IP は `ip addr` や `ifconfig` で調べる。

**全体表示画面は LAN の IP で開くこと。** そこに出る QR コードは、ブラウザが
実際に開いているアドレスから作られる。`localhost` で開くとメンバーのスマートフォンから
届かない URL になってしまう。QR の下に URL を出してあるので確かめられる。

どうしてもサーバと同じ PC で開きたい場合は、`.env` に次のように書く
（`.env.example` を参照）。

```
PUBLIC_BASE_URL=http://192.168.1.10:8000
```

DB は PostgreSQL のコンテナが一緒に立ち上がる。Vercel と同じ DB にしてあるのは
「ローカルでは動くのに本番で壊れる」を防ぐため。記録は `pgdata` ボリュームに入る。

SQLite で軽く動かしたいときは、`docker-compose.yml` の `web` の `DATABASE_URL` を
`sqlite:////app/data/app.db` に変えて `db` サービスを止めればよい。
アプリ側のコード変更は要らない。

### 開発

```bash
uv sync                                   # または python -m venv .venv && pip install -e ".[dev]"
uv run uvicorn app.main:app --reload --port 8000   # DB 未指定なら ./data/app.db (SQLite)
uv run pytest                             # テストはインメモリ SQLite
uv run ruff check .
```

## Vercel にデプロイする

タブレットが PC と同じ LAN にいなくても使えるようになる。
**SQLite は使えない**ので、Postgres を用意する（Vercel のサーバーレス関数は
ファイルシステムが揮発性で、リクエスト間でファイルが残らないため）。

1. **Postgres を用意する**
   Vercel は 2024年12月に自社の Postgres を終了し、いまは Marketplace 経由の
   Neon などを使う。Vercel のプロジェクト → Storage → Neon を追加すれば、
   接続文字列が環境変数として自動で入る。Neon の無料枠（0.5GB）で十分足りる。

2. **環境変数 `DATABASE_URL` を設定する**
   SQLAlchemy 用にドライバ名を付ける必要がある。Neon が渡す
   `postgres://...` や `postgresql://...` をそのままでは使えない。

   ```
   DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>/<db>?sslmode=require
   ```

3. **デプロイする**
   Vercel にリポジトリを取り込んであれば、`main` への push で本番、
   それ以外のブランチへの push でプレビューが作られる。
   `vercel.json` と `api/index.py` がリポジトリにあるので追加の設定は要らない。

4. **テーブルを作る**
   初回アクセス時に自動で作られる。明示的に作りたい場合は手元から
   `DATABASE_URL=... uv run python -m app.init_db` を実行する。

注意点:

- Neon の無料枠はアイドル時に自動停止するので、しばらく使っていないと
  最初のリクエストに数秒かかる。練習会が始まる前に一度開いておくとよい。
- 記録がクラウドに載る。練習会が終わったら管理画面から練習会を削除する。

SQLite のまま常設したい場合は、永続ディスクを持てる Fly.io や Render の方が素直。

## 合言葉

トップ画面とメンバー管理画面には合言葉が要る。既定は `thrivepickle`。

```bash
ADMIN_PASSWORD=すきな合言葉
```

変えたら再起動するだけでよい（DB は作り直さない）。古い端末のクッキーは無効になる。

**いたずら防止であって、秘密を守る仕組みではない。** 練習会のトークンで開く画面
（管理・全体表示・メンバー用）には掛からない。QR を読んだメンバーに合言葉を配る
わけにいかないため。**既定のまま公開ネットワークに出さないこと。**

## ほかの DB に差し替える

`DATABASE_URL` を変えるだけでよい。アプリは ORM しか使っておらず、
DB 固有の機能には依存していない。

```bash
DATABASE_URL=postgresql+psycopg://user:password@localhost/pickle
```

`docker-compose.yml` に PostgreSQL を足す例をコメントで入れてある。

## 更新するとき

ふだんは `docker compose up --build` で足ります。**DB は作り直しません。**

スキーマを変えたときだけ注意が必要です。`create_all` は足りないテーブルを作るだけで、
既存のテーブルには列を足しません（CLAUDE.md 不変則8）。テーブルを変えるときは
サービスを止めて、`scripts/` に置いた使い捨ての移行スクリプトを流してください。

## 記録を破棄する

管理画面から練習会を削除する。まるごと消すなら:

```bash
docker compose down -v      # -v で DB のボリュームごと消える
```

メンバー台帳（`people`）は練習会に属さないので、練習会を削除しても残る。
メンバー管理画面（トップから開く）で個別に消せる。
