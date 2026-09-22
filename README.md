# ピックルボール対戦カード生成アプリ

練習会の対戦カードを自動で組むウェブアプリ。仕様は `doc/spec.md`、開発手順は `PLAN.md`。

## 起動

```bash
docker compose up --build
```

`http://localhost:8000` を開く。タブレットからは、同じ LAN にいる PC の IP を使って
`http://<PC の IP>:8000` を開く。

## 記録の破棄

```bash
docker compose down
rm -rf data
```
