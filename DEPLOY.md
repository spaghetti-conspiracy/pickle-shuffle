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
最初の問い合わせで落ちる。逆に移行を先に流しておけば、古いコードは増えた列を
無視するだけなので落ちない。

移行スクリプトは古い列を**消さずに残す**。動作を確かめてから、落ち着いて消す。

---

## 戻したいとき

Vercel の Deployments で、前のデプロイの **Promote to Production** を押す。
コードは戻るが **DB は戻らない**ので、スキーマを変えた回は戻せないと思ったほうがよい
（Neon のブランチから復元することになる）。

---

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
