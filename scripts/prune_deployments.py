"""古いデプロイを消す。

``python scripts/prune_deployments.py [--dry-run]``

**残すのは、最新の READY な production と preview を1つずつだけ。**
それ以外（失敗したもの、古いもの）は消す。

Vercel は出したものをすべて残す。費用は増えない（関数は呼ばれたときだけ動く）が、
放っておくと困る:

- **どの URL も公開されていて、同じ DB に繋がる。** 環境変数は環境単位なので、
  古い Preview も**いまの staging の DB** を触る
- **古いコードのまま動く。** 列を変えたあとに叩かれると、おかしなデータが入る
- クローラが拾えば、そのたびに DB が5分起きる

要るもの（環境変数）:

    VERCEL_TOKEN       アクセストークン
    VERCEL_PROJECT_ID  prj_…
    VERCEL_ORG_ID      team_…
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.vercel.com"


def _call(method: str, path: str, token: str) -> dict:
    request = urllib.request.Request(f"{API}{path}", method=method)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as error:  # 消せなかった理由を見せる
        detail = error.read().decode()[:200]
        raise SystemExit(f"{method} {path}: {error.code} {detail}") from error
    return json.loads(body) if body else {}


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    token = os.environ.get("VERCEL_TOKEN")
    project = os.environ.get("VERCEL_PROJECT_ID")
    team = os.environ.get("VERCEL_ORG_ID")
    if not (token and project and team):
        raise SystemExit(
            "VERCEL_TOKEN / VERCEL_PROJECT_ID / VERCEL_ORG_ID を設定してください"
        )

    scope = f"projectId={project}&teamId={team}"
    found = _call("GET", f"/v6/deployments?{scope}&limit=100", token)
    deployments = found.get("deployments", [])
    # 新しい順に並んでいる前提だが、念のため自分で並べ替える。
    deployments.sort(key=lambda d: d.get("created", 0), reverse=True)

    keep: dict[str, str] = {}
    for item in deployments:
        target = item.get("target") or "preview"
        if item.get("readyState") == "READY" and target not in keep:
            keep[target] = item["uid"]

    if not keep:
        print("READY なデプロイが1つも無い。何もしない。")
        return
    print("残す:")
    for target, uid in keep.items():
        url = next(d["url"] for d in deployments if d["uid"] == uid)
        print(f"  {target:<10} {url}")

    doomed = [d for d in deployments if d["uid"] not in keep.values()]
    if not doomed:
        print("消すものは無い。")
        return

    print(f"消す（{len(doomed)} 件）:")
    for item in doomed:
        target = item.get("target") or "preview"
        print(f"  {item.get('readyState',''):<8} {target:<10} {item['url']}")
        if not dry_run:
            _call("DELETE", f"/v13/deployments/{item['uid']}?teamId={team}", token)
    if dry_run:
        print("\n--dry-run なので消していない。")


if __name__ == "__main__":
    main()
