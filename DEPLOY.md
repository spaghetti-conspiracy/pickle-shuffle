# デプロイ手順

2回目以降の手順。初回の準備は [DEPLOY_SETUP.md](DEPLOY_SETUP.md)。

デプロイの経路は **GitHub Actions の1本だけ**。Vercel の Git 連携は止めてある
（`vercel.json` の `git.deploymentEnabled: false`）。いつ何が出たかは、
GitHub の Actions の履歴だけを見れば分かる。

```
PR ──(CI が緑)──► main ──(CI が緑)──► staging
                              │
                              └──(release を打つ)──► 本番
```

---

## staging へ出す

**何もしなくてよい。** `main` に入れば自動で出る。

1. PR を出す。CI（lint / テスト / Vercel と同じ形で起動 / コンテナ）が緑になる
2. マージする
3. `main` の CI が走る。**ここではブラウザテストも走る**
4. 緑なら `Deploy staging` が動き、staging に出る
5. 続けて smoke が走り、出たものを叩いて確かめる

### 何を確かめているか

CI（ランナーの上）では**原理的に確かめられないもの**だけを、出したあとに叩く。

1. `/api/health` が応答する
2. 合言葉なしで `/api/sessions` が 401（門が開いていないこと）
3. staging の合言葉で 200（＝**環境変数が正しく、DB が読める**）
4. `member.html` と `api.js` が配られている（Vercel のルーティング）
5. 練習会を1つ作って消す（＝**DB に書ける。移行が流れている**）

5 で作る練習会は `ci-smoke-<実行ID>` という名前で、最後に必ず消す。
もし残っていたら、smoke が途中で落ちた跡なので管理画面から消してよい。

### 失敗したら

| どこで落ちたか | たいていの原因 |
|---|---|
| deploy | `VERCEL_TOKEN` の失効、Vercel 側のビルドエラー |
| smoke の 1〜2 | 出たものが起動していない。Vercel の Runtime Logs を見る |
| smoke の 3 | `DATABASE_URL` が違う。Neon が停止している |
| smoke の 5 | **移行の流し忘れ**（下の「スキーマを変えた回」） |

staging が壊れても本番には影響しない。直して `main` に入れ直せばよい。

---

## 本番へ出す

**release を打つ。** タグだけでは出ない（GitHub の Release を公開すると動く）。

```bash
git switch main && git pull
git tag v0.2.0
git push origin v0.2.0
gh release create v0.2.0 --title 'v0.2.0' --notes '変更の要点'
```

`Release` ワークフローが、次の順で動く:

1. **そのコミットの CI が緑かを確かめる**（`main` への push で走った CI のみを見る）。
   緑でなければここで止まる
2. タグを checkout して本番へ出す
3. `/api/health` が応答するか確かめる

### 合言葉について

**本番の合言葉は GitHub のどこにも無い。** Vercel の環境変数にしか存在せず、
release ワークフローは `/api/health` しか叩かない。

変えるときは Vercel 側で入れ直して、再デプロイするだけでよい（DB は触らない。
起動のたびに環境変数からハッシュを作り直す）。変えると**既存のクッキーは
すべて無効**になるので、全端末で入れ直しになる。

```bash
npx vercel env rm ADMIN_PASSWORD production
npx vercel env add ADMIN_PASSWORD production
```

### 打つ前に

- [ ] staging で動きを見たか
- [ ] `main` の CI が緑か（ブラウザテストを含む）
- [ ] **スキーマを変えた回なら、本番 DB に移行を流したか**（下記）

---

## スキーマを変えた回

`create_all` は**足りないテーブルを作るだけ**で、既存のテーブルに列を足さない
（CLAUDE.md 不変則8）。列やテーブルを変えたときは、**staging と本番の両方**に
移行を流す。

```bash
# 1. バックアップを取る（Neon はブランチを切っておくのが早い）
#    Neon の Branches → New Branch で、その時点のコピーを作る

# 2. staging に流す
DATABASE_URL='<staging の接続文字列>' uv run python scripts/migrate_XXX.py --dry-run
DATABASE_URL='<staging の接続文字列>' uv run python scripts/migrate_XXX.py

# 3. staging で動くことを確かめる

# 4. 本番に流す
DATABASE_URL='<本番の接続文字列>' uv run python scripts/migrate_XXX.py --dry-run
DATABASE_URL='<本番の接続文字列>' uv run python scripts/migrate_XXX.py

# 5. release を打つ
```

**順番が大事。** 先にコードを出すと、古いスキーマのまま新しいコードが動いて
最初の問い合わせで落ちる。

**新しいテーブルを足しただけなら、流さなくてよい。** 起動時に「管理者がいるか」を
見て、足りなければそのとき作る。列を変えたときだけ、上の移行スクリプトが要る。逆に移行を先に流しておけば、古いコードは増えた列を
無視するだけなので落ちない。

移行スクリプトは古い列を**消さずに残す**。動作を確かめてから、落ち着いて消す。

---

## 戻したいとき

Vercel の Deployments で、前のデプロイの **Promote to Production** を押す。
コードは戻るが **DB は戻らない**ので、スキーマを変えた回は戻せないと思ったほうがよい
（Neon のブランチから復元することになる）。

---

## 古いデプロイを消す

Vercel は出したものを**すべて残す**。放っておいても費用は増えない（関数は
呼ばれたときだけ動く）が、無害ではない。

- **どの URL も公開されていて、同じ DB に繋がる。** 環境変数は環境単位なので、
  古い Preview も**いまの staging の DB** を触る。URL を知っていれば誰でも操作できる
- **古いコードのまま動く。** 列を変えたあとに古いデプロイを叩かれると、
  おかしなデータが入ることもある
- クローラが拾えば、そのたびに DB が5分起きる

**最新の READY を production と preview で1つずつ残し、あとは消す**スクリプトを
用意してある。

```bash
export VERCEL_TOKEN='<token>'
export VERCEL_PROJECT_ID='prj_…'    # .vercel/project.json
export VERCEL_ORG_ID='team_…'

uv run python scripts/prune_deployments.py --dry-run   # 下見
uv run python scripts/prune_deployments.py             # 実行
```

手で消すなら CLI でもできる。

```bash
npx vercel@59.25.4 ls --token "$VERCEL_TOKEN"                      # 一覧
npx vercel@59.25.4 remove <URL> --yes --token "$VERCEL_TOKEN"      # 1つ消す
npx vercel@59.25.4 remove pickle-shuffle --safe --yes --token "$VERCEL_TOKEN"
```

`--safe` は**どのドメインからも参照されていないものだけ**を消す。

## デプロイは GitHub Actions からだけにする

Vercel の Git 連携は**残しておくと push だけでデプロイが走る**。Actions 経由の
デプロイと二重になり、どの版が出ているのか追えなくなる。止め方は2つあり、
**両方やる**（片方だけでは漏れた実績がある）。

1. `vercel.json`（リポジトリ側・設定済み）

   ```json
   "git": { "deploymentEnabled": false }
   ```

2. **Ignored Build Step**（プロジェクト設定・設定済み）

   Settings → Git → Ignored Build Step に `exit 0`。Vercel は
   **終了コード 0 = ビルドをスキップ**と解釈する。API からも設定できる。

   ```bash
   curl -X PATCH -H "Authorization: Bearer $VERCEL_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"commandForIgnoringBuildStep": "exit 0"}' \
     "https://api.vercel.com/v9/projects/$VERCEL_PROJECT_ID?teamId=$VERCEL_ORG_ID"
   ```

Actions からの `vercel deploy` はこの設定を通らないので、影響を受けない。
効いているかは、push したあと一覧に増えていないことで確かめる。

## よく使う vercel コマンド

```bash
V="npx vercel@59.25.4"                    # 版はワークフローと揃える

$V whoami --token "$VERCEL_TOKEN"         # トークンが誰のものか
$V ls --token "$VERCEL_TOKEN"             # デプロイの一覧
$V env ls --token "$VERCEL_TOKEN"         # 環境変数（値は出ない）
$V env add ADMIN_PASSWORD production      # 値を対話で入れる（履歴に残らない）
$V logs <URL> --token "$VERCEL_TOKEN"     # 実行時のログ（500 の原因はここ）
$V inspect <URL> --logs --token "$VERCEL_TOKEN"   # ビルドのログ
```

**トークンは "All projects" で作る。** 特定のプロジェクトに絞ると CLI では
`Could not retrieve Project Settings` で動かない（DEPLOY_SETUP.md）。

**出力の形は環境で変わる。** 手元では JSON、CI（`CI=true`）では平文。
スクリプトで URL を拾うときは、両方を受けるようにする。

## 確認に使うコマンド

```bash
# いまの CI の状態
gh run list --workflow CI --limit 5

# 本番と staging が生きているか
curl -s https://<PRODUCTION_DOMAIN>/api/health
curl -s https://<STAGING_DOMAIN>/api/health

# 合言葉の門が開いていないか（401 が返るのが正しい）
curl -s -o /dev/null -w '%{http_code}\n' https://<PRODUCTION_DOMAIN>/api/sessions
```
