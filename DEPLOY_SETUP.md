# 初回セットアップ

Vercel に import するところから、Neon で DB を用意して、staging と本番に
一通り出すまで。**一度だけの手順**。2回目以降は [DEPLOY.md](DEPLOY.md)。

前提:

- GitHub のリポジトリがあること
- **CI とデプロイのワークフローが `main` に入っていること**（`.github/workflows/`）
- Vercel と Neon のアカウント（どちらも無料枠で足りる）

順番が大事な箇所が1つある。**Vercel に import する前に、ワークフローと
`vercel.json` が `main` に入っていること。** 先に import すると、設定が何も無い
状態で初回のデプロイが走り、壊れたものが本番 URL に出る。

---

## 0. CLI を使えるようにする

以降の `npx vercel …` は、**手元のマシンのターミナルで、このリポジトリの
ルート**で実行する（`vercel link` がそこに `.vercel/project.json` を作るため）。

- **Node 18 以上**が要る。`node -v` で確認する
- `npx vercel login` はブラウザを開くので、画面のあるマシンで実行する

Ubuntu 22.04 の apt が入れる Node は **v12** で古すぎる。どちらかで入れる。

```bash
# NodeSource のリポジトリから（システムに入る）
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# または nvm（sudo が要らない。ユーザ単位）
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
. ~/.nvm/nvm.sh && nvm install 20
```

CLI 本体は入れても入れなくてもよい。

```bash
npm install -g vercel     # 入れる場合
npx vercel@59.25.4 --version   # 入れない場合。ワークフローと同じ版
```

Node を入れたくなければ、環境変数はすべて Vercel の画面から入れてもよい
（合言葉は **Sensitive** を選ぶ）。その場合 `orgId` / `projectId` は
Project Settings → General と Team Settings から読める。

## 1. Neon で DB を用意する

1. [Neon](https://neon.tech) でプロジェクトを作る（Region は Vercel の関数と
   近いところ。日本なら `ap-southeast-1` など）
2. **ブランチを2つにする。** Neon はデータベースをブランチできる
   - `main` … 本番用（プロジェクト作成時にできている）
   - `staging` … Branches → New Branch で `main` から分岐して作る
3. それぞれの接続文字列を取る。Dashboard → Connection Details で**ブランチを選ぶ**
   - **Pooled connection**（ホスト名に `-pooler` が付く方）を選ぶ。
     サーバーレスは関数インスタンスが多数同時に立つので、接続の面倒は
     Neon 側の PgBouncer に見てもらう（アプリは `NullPool` で自前のプールを持たない）
4. SQLAlchemy 用にドライバ名を付ける。**Neon が出すままの `postgres://` では動かない**

   ```
   postgresql+psycopg://<user>:<password>@<host>-pooler.<region>.aws.neon.tech/<db>?sslmode=require
   ```

この2本（本番用・staging 用）を控えておく。

## 2. Vercel にプロジェクトを作る

1. Add New → Project → GitHub のリポジトリを import
2. **Framework Preset: Other**。Build Command などは空のままでよい
   （`vercel.json` と `api/index.py` がある）
3. **Deploy を押す前に環境変数を入れる。** 入れるのは次の2つで、
   **Production と Preview（= staging）で別々の値**にする。

   | 変数 | Production | Preview（= staging）|
   |---|---|---|
   | `DATABASE_URL` | Neon の **main** ブランチ | Neon の **staging** ブランチ |
   | `ADMIN_PASSWORD` | 本番の合言葉 | staging の合言葉 |

   `PUBLIC_BASE_URL` は設定しない（ブラウザが叩いたホストが QR に入る）。

   **合言葉は CLI から入れる。** 値を対話で聞かれるので、画面にも
   シェルの履歴にも残らない。

   ```bash
   npx vercel login
   npx vercel link                              # 対話でプロジェクトを選ぶ
   npx vercel env add ADMIN_PASSWORD production # 本番の合言葉
   npx vercel env add ADMIN_PASSWORD preview    # staging の合言葉（別の値にする）
   npx vercel env add DATABASE_URL production
   npx vercel env add DATABASE_URL preview
   ```

   画面から入れる場合は、保存時に **Sensitive** を選ぶと、保存後は画面からも
   API からも読み出せなくなる（プランにより可否があるので画面で確認する）。

4. Deploy を押す。**この1回だけは Vercel の Git 連携が動く**

## 3. Vercel の自動デプロイを止める

デプロイの経路を **GitHub Actions の1本だけ**にする。理由:

- Vercel の Git 連携は**ブランチへの push でしか動かず、タグでは発火しない**。
  「release を打ったら本番」を実現するには Actions から出すしかない
- 連携を残すと、`main` に入った瞬間に本番へ出てしまう
- どのブランチの Preview も **staging の DB に書き込む**（Preview の環境変数は
  全 Preview で共通）

止め方は2つ。**両方やる。**

1. `vercel.json` に設定が入っている（リポジトリ側・設定済み）

   ```json
   "git": { "deploymentEnabled": false }
   ```

   これは **Production Branch の `vercel.json` が読まれる**ので、`main` に
   入ってから効く

2. ダッシュボードでも止める（1 が効くまでの保険）
   Settings → Git → **Ignored Build Step** に次を入れる

   ```bash
   exit 0
   ```

   Vercel の Ignored Build Step は「終了コード 0 = ビルドをスキップ」。
   **これが効くのは Git の push で始まったデプロイだけ**で、Actions から
   `vercel deploy` で始めたものには掛からない

## 4. Actions から出すための値を取る

手順2で `vercel link` してあるので、ID はもうファイルにある。

```bash
cat .vercel/project.json   # orgId と projectId
```

トークンは**アカウントの設定**から発行する（プロジェクトの設定ではない）。

1. <https://vercel.com/account/tokens> を開く
   （アバター → **Settings** → **Tokens** でも同じ）
2. **Create Token**
3. 入力するのは3つ
   - **Token Name**: `github-actions-pickle-shuffle` など、用途が分かる名前
   - **Scope**: **チーム（`spaghetti-conspiracy`）を選び、対象は "All projects" にする。**
     **特定のプロジェクトに絞ると CLI では使えない。** 絞ったトークンは REST API で
     プロジェクトを引くことはできるが、ユーザを引けないため（`/v2/user` が
     `User not found`）、CLI が最初のユーザ読み込みで落ちる:

     ```
     Error: Not able to load user because of unexpected error: User not found. (404)
     Error: Could not retrieve Project Settings.
     ```

   - **Expiration**: 期限。切れるとデプロイが止まるので、付けるなら控えておく
4. **Create**。**値はこの1回しか表示されない**

作ったトークンが使えるかは、これで確かめられる（ユーザ名が返れば CLI で使える）。

```bash
curl -s -H "Authorization: Bearer <token>" https://api.vercel.com/v2/user
```

`vercel login` 済みなら手元の `~/.local/share/com.vercel.cli/auth.json`
（環境により `~/.vercel/auth.json`）にもトークンがあるが、**CI 用は別に
発行する**。漏れたときに、そちらだけ失効させれば済む。

`.vercel/` は `.gitignore` に入っているのでコミットされない。

## 5. GitHub に登録する

Secrets:

```bash
gh secret set VERCEL_TOKEN --body '<token>'
gh secret set VERCEL_ORG_ID --body '<orgId>'
gh secret set VERCEL_PROJECT_ID --body '<projectId>'
gh secret set STAGING_ADMIN_PASSWORD --body '<staging の合言葉>'
```

**本番の合言葉は GitHub に置かない。** 置いてあるのは staging のぶんだけで、
これは出したあとの staging にログインして「DB に書けること（＝移行が
流れていること）」を確かめるために要る。

本番に対しては**合言葉の要らない経路しか叩かない**（health、存在しない練習会を
引いて DB 到達を見る、門が閉じているか、メンバー用画面が配られているか）。
したがって本番の合言葉は **Vercel の環境変数にしか存在しない**。

| 合言葉 | Vercel | GitHub | DB |
|---|---|---|---|
| 本番 | ある（Production スコープ） | **無い** | ハッシュのみ |
| staging | ある（Preview スコープ） | Secrets にある | ハッシュのみ |

staging と本番で**必ず別の合言葉にすること**。同じにすると、GitHub に置いた
時点で本番の合言葉も置いたことになる。

Variables（**スキームは付けない**。ホスト名だけ）:

```bash
gh variable set STAGING_DOMAIN --body 'pickle-shuffle-staging.vercel.app'
gh variable set PRODUCTION_DOMAIN --body 'pickle-shuffle.vercel.app'

# gh が 2.36 より古いとき（`unknown command "variable"` になる）
gh api -X POST repos/<owner>/<repo>/actions/variables \
  -f name=STAGING_DOMAIN -f value=pickle-shuffle-staging.vercel.app
```

**`STAGING_DOMAIN` は自分で決める。** Vercel の Git 連携を止めてあるので、
`pickle-shuffle-git-main-….vercel.app` のようなブランチ用の固定 URL は作られない。
デプロイごとの URL（`pickle-shuffle-<英数字>-….vercel.app`）は毎回変わるので、
**固定の別名を1つ決めて、ワークフローに張り替えさせる**。

```bash
gh variable set STAGING_DOMAIN --body 'pickle-shuffle-staging.vercel.app'
```

`gh variable` は **gh 2.36 以降**。古い `gh`（Ubuntu 22.04 の apt は 2.4.0）では
`gh api` を使う。

```bash
# 新しく作るとき
gh api -X POST repos/<owner>/<repo>/actions/variables \
  -f name=STAGING_DOMAIN -f value=pickle-shuffle-staging.vercel.app

# すでにあるものを変えるとき（POST だと 409 になる）
gh api -X PATCH repos/<owner>/<repo>/actions/variables/STAGING_DOMAIN \
  -f name=STAGING_DOMAIN -f value=<新しい値>
```

`*.vercel.app` の空いている名前なら何でもよい。設定すると、以後のデプロイで
`vercel alias set` が走り、その名前でいつでも staging を開けるようになる
（未設定でも deploy 自体は動き、確認だけが省かれる）。

## 6. main を守る

CI が緑でなければ `main` に入れられないようにする。

```bash
gh api -X PUT repos/<owner>/<repo>/branches/main/protection \
  -F 'required_status_checks[strict]=true' \
  -f 'required_status_checks[contexts][]=テスト (Python 3.10)' \
  -f 'required_status_checks[contexts][]=テスト (Python 3.12)' \
  -f 'required_status_checks[contexts][]=Vercel と同じ形で起動する' \
  -f 'required_status_checks[contexts][]=本番と同じ DB で通す' \
  -f 'required_status_checks[contexts][]=コンテナが組み上がる' \
  -F 'enforce_admins=true' \
  -F 'required_pull_request_reviews=null' \
  -F 'restrictions=null'
```

boolean は `-F`、文字列は `-f`。`enforce_admins=true` にしないと、管理者は
保護を素通りできる（1人で運用するなら、ここを true にしないと意味が無い）。

**ブラウザテストは必須チェックに入れない。** PR では走らないので（release の
前提として `main` への push でだけ走る）、必須にすると永久にブロックされる。

## 7. staging に出す

`main` に何か push するか、Actions から CI を re-run する。CI が緑になると
`Deploy staging` が動く。

出たら:

1. Vercel の Deployments で URL を確認し、`STAGING_DOMAIN` に設定する
2. staging を開いて、合言葉を入れて練習会を1つ作ってみる
3. **DB は起動時に自動で用意される。** 初回アクセスのとき、管理者がいなければ
   テーブルと団体・管理者を作る（確認そのものは1往復で済むので、ふだんの
   起動は遅くならない）。

   先に作っておきたい場合や、作られたことを確かめたい場合は手元から:

   ```bash
   DATABASE_URL='<staging の接続文字列>' uv run python -m app.init_db
   DATABASE_URL='<本番の接続文字列>'     ADMIN_PASSWORD='<本番の合言葉>' uv run python -m app.init_db
   ```

   テーブルと、既定の団体・管理者がまとめて用意される。何度実行してもよい。

   **合言葉を変えたときは、DB を触らなくてよい。** Vercel の環境変数を
   入れ直して再デプロイすれば、次のログインから新しい合言葉で入れる
   （合わなかったときだけ作り直すので、ふだんの起動は遅くならない）。

   すでに開いている端末のクッキーは、**この作り直しが起きた時点で**切れる。
   合言葉を変えた本人が新しい合言葉で入り直せば、そのログインが作り直しを
   起こすので、そこで古い端末は締め出される。誰もログインしないうちは
   古いクッキーが通るので、確実に切りたいときは自分で1回ログインしておく。

`STAGING_DOMAIN` を設定すると、次のデプロイから smoke（健全性の確認）が走る。

## 8. 本番に出す

```bash
git tag v0.1.0
git push origin v0.1.0
gh release create v0.1.0 --title 'v0.1.0' --notes '試験運用の開始'
```

`Release` ワークフローが、**そのコミットの CI が緑か**を確かめてから本番へ出す。

---

## うまくいかないとき

| 症状 | 見るところ |
|---|---|
| 本番/staging が 500 | `DATABASE_URL` のドライバ名（`postgresql+psycopg://`）と `sslmode=require` |
| 最初のアクセスだけ遅い | Neon の無料枠はアイドルで自動停止する。練習会の前に一度開く |
| `prepared statement ... does not exist` | 接続文字列に `&prepare_threshold=0` を足す（PgBouncer 対策） |
| push しただけで Vercel が動く | 手順3が効いていない。Ignored Build Step を確認 |
| `Could not retrieve Project Settings` | トークンが特定プロジェクトに絞られている。**All projects** で作り直す |
| Actions の deploy が 403 | `VERCEL_TOKEN` のスコープにこのプロジェクトのチームが入っているか |
| staging に `ci-smoke-…` が残っている | smoke の片付けが失敗した残骸。管理画面から消してよい |
