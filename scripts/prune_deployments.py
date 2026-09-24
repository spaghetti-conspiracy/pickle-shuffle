"""古いデプロイを消す。手で実行する（CI からは呼ばない）。

``python scripts/prune_deployments.py [--dry-run]``

**外向けの URL（エイリアス）が指しているデプロイを残し、それ以外を消す。**
`pickle-shuffle.vercel.app`（本番）や `pickle-shuffle-staging.vercel.app`（staging）の
ように、ドメインが紐づいているものは使われているので残す。

「環境ごとに最新の1つ」だけで選ぶと、ロールバックした直後のように外向けの URL が
古い方を指しているとき、使われているデプロイを消してしまう。

消し過ぎないよう、次のものも残す（残す理由はすべて表示する）:

- 状態が「消してよい状態」（READY / ERROR / CANCELED）でないもの。ビルド中・待機中や、
  Vercel が今後増やす知らない状態のものを消さない
- 環境（production / preview）ごとの最新の READY。staging に固定ドメインを付けない設定
  （`STAGING_DOMAIN` 未設定）でも、今の staging を消さないための保険
- 作成から10分以内のもの。staging は出してから数秒後にエイリアスを付けるので、その隙間で消さない

消す直前にも、そのデプロイ自身のエイリアスを取り直し、1つでもあれば飛ばす
（一覧のページの境目で取りこぼしても、使われているものを消さない）。
エイリアスが1つも取れなければ何もしない（取得の失敗で全部消えないように）。

**実行すると、ロールバック先（エイリアスの付いていない古い本番デプロイ）も無くなる。**

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
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable

API = "https://api.vercel.com"

DELETABLE = {"READY", "ERROR", "CANCELED"}
"""消してよい状態。これ以外（ビルド中・待機中・BLOCKED・知らない状態）は残す。"""

GONE = "DELETED"
"""すでに消えている。残すとも消すとも扱わない。"""

RECENT_MS = 10 * 60 * 1000
"""作成からこれ以内のデプロイは残す。エイリアスを付ける前の隙間で消さないため。"""


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
    """エイリアスが指しているデプロイの id。リダイレクトだけのエイリアスでは None。"""
    return alias.get("deploymentId") or (alias.get("deployment") or {}).get("id")


def choose(
    deployments: Iterable[dict], aliases: Iterable[dict], now_ms: int
) -> tuple[dict[str, list[str]], list[dict]]:
    """残すデプロイ（id → 残す理由）と、消すデプロイを決める。"""
    deployments = sorted(deployments, key=lambda d: d.get("created", 0), reverse=True)
    keep: dict[str, list[str]] = {}

    def remember(uid: str, reason: str) -> None:
        keep.setdefault(uid, []).append(reason)

    for alias in aliases:
        uid = deployment_of(alias)
        if uid:
            remember(uid, alias.get("alias", "?"))

    latest_seen: set[str] = set()
    for item in deployments:
        state = item.get("readyState")
        target = item.get("target") or "preview"
        if state == GONE:
            continue
        if state not in DELETABLE:
            remember(item["uid"], f"状態 {state}（消してよい状態ではない）")
        if state == "READY" and target not in latest_seen:
            latest_seen.add(target)
            remember(item["uid"], f"{target} の最新")
        if now_ms - item.get("created", 0) < RECENT_MS:
            remember(item["uid"], "作成から10分以内")

    doomed = [d for d in deployments if d["uid"] not in keep and d.get("readyState") != GONE]
    return keep, doomed


def still_aliased(get: Callable[[str], dict], uid: str, team: str) -> bool:
    """消す直前に、そのデプロイにエイリアスが付いていないかを取り直す。"""
    found = get(f"/v2/deployments/{uid}/aliases?teamId={team}")
    return bool(found.get("aliases"))


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

    keep, doomed = choose(deployments, aliases, now_ms=int(time.time() * 1000))
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
        line = f"  {item.get('readyState', ''):<8} {target:<10} {item.get('url', item['uid'])}"
        if still_aliased(get, item["uid"], team):
            print(f"{line}  ← 直前に見るとエイリアスが付いていたので残す")
            continue
        print(line)
        if not dry_run:
            _call("DELETE", f"/v13/deployments/{item['uid']}?teamId={team}", token)
    if dry_run:
        print("\n--dry-run なので消していない。")


if __name__ == "__main__":
    main()
