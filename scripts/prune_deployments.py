"""古いデプロイを消す。手で実行する（CI からは呼ばない）。

``python scripts/prune_deployments.py [--dry-run]``

**残すのは、外向けの URL（エイリアス）が指しているデプロイだけ。**
`pickle-shuffle.vercel.app`（本番）や `pickle-shuffle-staging.vercel.app`（staging）の
ように、ドメインが紐づいているものは使われているので残す。それ以外（古いもの、
失敗したもの）は消す。

「環境ごとに最新の1つ」で選ぶと、ロールバックした直後のように外向けの URL が
古い方を指しているとき、使われているデプロイを消してしまう。

ビルド中・待機中のデプロイも残す。リリースの途中で、まだエイリアスが付く前の
ものを消さないため。エイリアスが1つも取れなければ何もしない（取得の失敗で
全部消えないように）。

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
from collections.abc import Callable, Iterable

API = "https://api.vercel.com"

IN_PROGRESS = {"QUEUED", "INITIALIZING", "BUILDING"}
"""まだ終わっていないデプロイの状態。エイリアスが付く前かもしれないので消さない。"""


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


def _fetch_all(get: Callable[[str], dict], path: str, key: str) -> list[dict]:
    """ページ送りをたどって、一覧をすべて取る。1回100件まで。"""
    items: list[dict] = []
    until = None
    while True:
        page = get(f"{path}&limit=100" + (f"&until={until}" if until else ""))
        items.extend(page.get(key, []))
        until = (page.get("pagination") or {}).get("next")
        if not until:
            return items


def deployment_of(alias: dict) -> str | None:
    """エイリアスが指しているデプロイの id。"""
    return alias.get("deploymentId") or (alias.get("deployment") or {}).get("id")


def choose(
    deployments: Iterable[dict], aliases: Iterable[dict]
) -> tuple[dict[str, list[str]], list[dict]]:
    """残すデプロイ（id → 残す理由）と、消すデプロイを決める。

    残すのは、外向けの URL が指しているものと、まだ終わっていないもの。
    """
    keep: dict[str, list[str]] = {}
    for alias in aliases:
        uid = deployment_of(alias)
        if uid:
            keep.setdefault(uid, []).append(alias.get("alias", "?"))
    doomed = []
    for item in deployments:
        if item["uid"] in keep:
            continue
        if item.get("readyState") in IN_PROGRESS:
            keep[item["uid"]] = [f"{item['readyState']}（終わっていない）"]
            continue
        doomed.append(item)
    return keep, doomed


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    token = os.environ.get("VERCEL_TOKEN")
    project = os.environ.get("VERCEL_PROJECT_ID")
    team = os.environ.get("VERCEL_ORG_ID")
    if not (token and project and team):
        raise SystemExit(
            "VERCEL_TOKEN / VERCEL_PROJECT_ID / VERCEL_ORG_ID を設定してください"
        )

    def get(path: str) -> dict:
        return _call("GET", path, token)

    scope = f"projectId={project}&teamId={team}"
    deployments = _fetch_all(get, f"/v6/deployments?{scope}", "deployments")
    aliases = _fetch_all(get, f"/v4/aliases?{scope}", "aliases")

    if not aliases:
        print("外向けの URL（エイリアス）が1つも取れない。何もしない。")
        return

    keep, doomed = choose(deployments, aliases)
    urls = {d["uid"]: d.get("url", d["uid"]) for d in deployments}
    print("残す:")
    for uid, reasons in keep.items():
        print(f"  {urls.get(uid, uid)}  ← {', '.join(reasons)}")

    if not doomed:
        print("消すものは無い。")
        return
    print(f"消す（{len(doomed)} 件）:")
    for item in doomed:
        target = item.get("target") or "preview"
        print(f"  {item.get('readyState', ''):<8} {target:<10} {item.get('url', item['uid'])}")
        if not dry_run:
            _call("DELETE", f"/v13/deployments/{item['uid']}?teamId={team}", token)
    if dry_run:
        print("\n--dry-run なので消していない。")


if __name__ == "__main__":
    main()
