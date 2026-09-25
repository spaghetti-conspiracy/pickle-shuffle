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
uv sync                                   # uv が前提（開発用の依存は pyproject.toml の [dependency-groups] にあり、pip install -e ".[dev]" では入らない）
uv run uvicorn app.main:app --reload --port 8000   # DB 未指定なら ./data/app.db (SQLite)
uv run pytest                             # テストはインメモリ SQLite
uv run ruff check .
```

## デプロイ

タブレットが PC と同じ LAN にいなくても使えるようになる。
**SQLite は使えない**ので Postgres（Neon）を使う。Vercel のサーバーレス関数は
ファイルシステムが揮発性で、リクエスト間でファイルが残らない。

- **初回の準備**（Vercel への import、Neon の用意、Actions の設定）:
  [DEPLOY_SETUP.md](DEPLOY_SETUP.md)
- **2回目以降**（staging と本番への出し方、スキーマを変えた回の手順）:
  [DEPLOY.md](DEPLOY.md)

デプロイの経路は **GitHub Actions の1本だけ**。Vercel の Git 連携は
`vercel.json` の `git.deploymentEnabled: false` で止めてある。

SQLite のまま常設したい場合は、永続ディスクを持てる Fly.io や Render の方が素直。

## CI とデプロイ

GitHub Actions で三段にしてある。

| いつ | 何が走るか | 何のため |
|---|---|---|
| **PR** | lint / テスト（3.10・3.12）/ Vercel と同じ形で起動 / コンテナ | main に入れてよいかの判断 |
| **main への push** | 上記 **＋ ブラウザで主要な径路** | release を打ってよいかの判断。通ったら staging へ出て、**出したものを叩いて確かめる** |
| **release を打つ** | そのコミットの CI が green か確かめてから本番へ | 赤いコミットに release を打ってしまう事故を防ぐ |

### 用意するもの

GitHub の Secrets:

| 名前 | 中身 |
|---|---|
| `VERCEL_TOKEN` | Vercel のアクセストークン |
| `VERCEL_ORG_ID` / `VERCEL_PROJECT_ID` | `vercel link` すると `.vercel/project.json` に出る |
| `STAGING_ADMIN_PASSWORD` | staging の合言葉。出したあとの確認に使う |

GitHub の Variables（任意。設定すると出したあとに応答を確かめる）:
`STAGING_DOMAIN` / `PRODUCTION_DOMAIN`。

**main を守る設定**（ブランチ保護）と Vercel / Neon の用意は
[DEPLOY_SETUP.md](DEPLOY_SETUP.md) にまとめてある。

### 出したあとに何を確かめているか

staging へ出したら、**CI では原理的に確かめられないもの**だけを叩いて確かめる。

1. `/api/health` が応答する
2. 合言葉なしで `/api/sessions` が **401**（門が開いていないこと）
3. staging の合言葉で **200**（＝環境変数が正しく、**DB が読めている**）
4. `member.html` と `api.js` が配られている（Vercel のルーティングと `includeFiles`）
5. 練習会を1つ作って消す（**DB の書き込みと、移行の状態**まで確かめる）

5 を入れているのは、**列を足したのに移行を流し忘れると、起動は成功して最初の
問い合わせで落ちる**ため。作る練習会の名前には実行 ID を付け、失敗しても
必ず消す（`if: always()`）。

**フルのブラウザテストは staging に当てない。** データが汚れるうえ、人が
触っている最中とぶつかる。画面の検証は main への push のときに、CI 内に
立てたサーバへ当てている。

### Vercel と Neon の設定

**Vercel の自動デプロイは `vercel.json` で止めてある。**

```json
"git": { "deploymentEnabled": false }
```

Vercel の Git 連携は**ブランチへの push でしか動かず、タグでは発火しない**。
「release を打ったら本番」を実現するには Actions から `vercel deploy --prod` を
叩くしかないので、**staging も本番も Actions の1本に寄せてある**。自動デプロイを
残すと、main に入った瞬間に本番へ出たり、どのブランチの Preview も
**staging の DB に書き込んだり**する（Preview の環境変数は全 Preview で共通）。

そのぶん、いつ何が出たかは GitHub の実行履歴だけで追える。

環境変数は環境ごとに分ける。**`DATABASE_URL` を分け忘れると、staging の操作が
本番の記録を壊す。**

| 変数 | Production | Preview（= staging）|
|---|---|---|
| `DATABASE_URL` | 本番の DB | **別の DB**（Neon ならブランチを切る）|
| `ADMIN_PASSWORD` | 本番の合言葉 | staging の合言葉 |

Preview のデプロイ URL は既定で誰でも開ける（制限は Vercel の Deployment
Protection）。staging に実在のメンバー名を入れるときは意識すること。

### release を打つときの手順

1. main の CI が green であることを確かめる
2. **列やテーブルを変えた回だけ**、本番 DB に移行スクリプトを流す（下の「更新するとき」）
3. GitHub で release を打つ。`release.yml` が CI を確かめてから本番へ出す

### ブラウザテスト

ふだんの `pytest` では走らない。走らせるとき:

```bash
uv run playwright install chromium   # ブラウザ本体。playwright 自体は dev 依存に入っている
uv run pytest -m browser
```

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

### Phase 11（メンバー台帳の分離）の移行

既存の DB があるなら、**1度だけ**次を実行します。

```bash
docker compose exec db pg_dump -U pickle pickle > backup.sql   # 先にバックアップ
docker compose stop web
DATABASE_URL=... .venv/bin/python scripts/migrate_phase11.py   # --dry-run で下見できる
docker compose up -d
```

`member_profiles` の属性が `people` に移り、参加者が台帳に繋がります。
古い列（`tennisbear_*`）と `member_profiles` は**消さずに残す**ので、
動作を確かめてから落ち着いて消せます。

## 記録を破棄する

管理画面から練習会を削除する。まるごと消すなら:

```bash
docker compose down -v      # -v で DB のボリュームごと消える
```

メンバー台帳（`people`）は練習会に属さないので、練習会を削除しても残る。
メンバー管理画面（トップから開く）で個別に消せる。
