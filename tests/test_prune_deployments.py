"""古いデプロイを消すスクリプト（scripts/prune_deployments.py）の判定。

Vercel には繋がない。一覧を渡して、残す・消すの判定だけを確かめる。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "prune_deployments.py"
_spec = importlib.util.spec_from_file_location("prune_deployments", _PATH)
prune = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prune)

HOUR = 60 * 60 * 1000
NOW = 100 * HOUR
"""判定の「いま」。作成時刻はこれより前にする。"""


def deployment(
    uid: str, created: int, state: str | None = "READY", target: str | None = None
) -> dict:
    return {
        "uid": uid,
        "created": created,
        "readyState": state,
        "target": target,
        "url": f"{uid}.vercel.app",
    }


def alias(name: str, uid: str | None) -> dict:
    return {"alias": name, "deploymentId": uid}


def removed(deployments, aliases) -> list[str]:
    _keep, doomed = prune.choose(deployments, aliases, now_ms=NOW)
    return sorted(d["uid"] for d in doomed)


def test_what_an_external_url_points_to_is_kept_even_if_it_is_old():
    """ロールバック直後のように、外向けの URL が古い方を指していればそちらを残す。

    「環境ごとに最新の1つ」だけで選ぶと、使われているデプロイを消してしまう。
    """
    deployments = [
        deployment("new-prod", NOW - 1 * HOUR, target="production"),
        deployment("old-prod", NOW - 5 * HOUR, target="production"),  # 本番の URL が指す
        deployment("older-prod", NOW - 9 * HOUR, target="production"),
    ]
    keep, doomed = prune.choose(
        deployments, [alias("pickle-shuffle.vercel.app", "old-prod")], now_ms=NOW
    )
    assert keep["old-prod"] == ["pickle-shuffle.vercel.app"]
    assert [d["uid"] for d in doomed] == ["older-prod"]


def test_old_deployments_nobody_points_to_are_removed():
    deployments = [
        deployment("prod", NOW - 1 * HOUR, target="production"),
        deployment("prod-old", NOW - 5 * HOUR, target="production"),
        deployment("stg", NOW - 2 * HOUR),
        deployment("stg-old", NOW - 6 * HOUR),
        deployment("stg-broken", NOW - 7 * HOUR, state="ERROR"),
        deployment("stg-cancelled", NOW - 8 * HOUR, state="CANCELED"),
    ]
    aliases = [
        alias("pickle-shuffle.vercel.app", "prod"),
        alias("pickle-shuffle-staging.vercel.app", "stg"),
    ]
    assert removed(deployments, aliases) == ["prod-old", "stg-broken", "stg-cancelled", "stg-old"]


def test_the_latest_staging_survives_without_a_staging_domain():
    """STAGING_DOMAIN を設定せず staging にエイリアスが無くても、今の staging は残す。

    エイリアスだけで選ぶと、本番のエイリアスがあるので防御を通り抜け、
    preview は最新も含めて全部消えていた。
    """
    deployments = [
        deployment("prod", NOW - 1 * HOUR, target="production"),
        deployment("stg-latest", NOW - 2 * HOUR),
        deployment("stg-old", NOW - 5 * HOUR),
    ]
    keep, doomed = prune.choose(
        deployments, [alias("pickle-shuffle.vercel.app", "prod")], now_ms=NOW
    )
    assert keep["stg-latest"] == ["preview の最新"]
    assert [d["uid"] for d in doomed] == ["stg-old"]


@pytest.mark.parametrize(
    "state", ["QUEUED", "INITIALIZING", "BUILDING", "BLOCKED", "SOMETHING_NEW", None]
)
def test_only_finished_deployments_are_removed(state):
    """消してよいのは READY / ERROR / CANCELED だけ。

    ビルド中・待機中や、Vercel が今後増やす知らない状態のものは残す。
    """
    deployments = [
        deployment("prod", NOW - 1 * HOUR, target="production"),
        deployment("odd", NOW - 5 * HOUR, state=state),
    ]
    keep, doomed = prune.choose(
        deployments, [alias("pickle-shuffle.vercel.app", "prod")], now_ms=NOW
    )
    assert "odd" in keep
    assert doomed == []


def test_an_already_deleted_deployment_is_left_alone():
    """DELETED は消し直さない（削除を送って失敗すると途中で止まる）。"""
    deployments = [
        deployment("prod", NOW - 1 * HOUR, target="production"),
        deployment("gone", NOW - 5 * HOUR, state="DELETED"),
    ]
    keep, doomed = prune.choose(
        deployments, [alias("pickle-shuffle.vercel.app", "prod")], now_ms=NOW
    )
    assert "gone" not in keep
    assert doomed == []


def test_a_brand_new_deployment_is_kept():
    """staging は出してから数秒後にエイリアスを付ける。その隙間で消さない。"""
    deployments = [
        deployment("stg-new", NOW - 60_000),  # 1分前。まだエイリアスが無い
        deployment("stg", NOW - 3 * HOUR),
        deployment("stg-old", NOW - 5 * HOUR),
    ]
    keep, doomed = prune.choose(
        deployments, [alias("pickle-shuffle-staging.vercel.app", "stg")], now_ms=NOW
    )
    assert "作成から10分以内" in keep["stg-new"]
    assert [d["uid"] for d in doomed] == ["stg-old"]


def test_an_alias_with_a_nested_deployment_is_understood():
    """エイリアスが deployment を入れ子で持つ形でも、指している先を読める。"""
    deployments = [
        deployment("prod", NOW - 1 * HOUR, target="production"),
        deployment("live", NOW - 5 * HOUR, target="production"),
        deployment("old", NOW - 9 * HOUR, target="production"),
    ]
    aliases = [{"alias": "pickle-shuffle.vercel.app", "deployment": {"id": "live"}}]
    assert removed(deployments, aliases) == ["old"]


def test_a_redirect_only_alias_keeps_nothing():
    """deploymentId が空のエイリアス（リダイレクトだけ）は、何も残す理由にならない。"""
    deployments = [
        deployment("prod", NOW - 1 * HOUR, target="production"),
        deployment("old", NOW - 5 * HOUR, target="production"),
    ]
    aliases = [alias("pickle-shuffle.vercel.app", "prod"), alias("www.example.com", None)]
    assert removed(deployments, aliases) == ["old"]


def test_one_deployment_can_be_kept_for_several_urls():
    deployments = [deployment("live", NOW - 1 * HOUR, target="production")]
    aliases = [alias("pickle-shuffle.vercel.app", "live"), alias("pickle.example.com", "live")]
    keep, _doomed = prune.choose(deployments, aliases, now_ms=NOW)
    assert keep["live"][:2] == ["pickle-shuffle.vercel.app", "pickle.example.com"]


@pytest.mark.parametrize("key", ["deployments", "aliases"])
def test_every_page_of_a_listing_is_fetched(key):
    """100件を超えても、ページ送りをたどって全部取る。"""
    pages = {
        "/x?s&limit=100": {key: [{"uid": "a"}], "pagination": {"next": 5}},
        "/x?s&limit=100&until=5": {key: [{"uid": "b"}], "pagination": {"next": None}},
    }
    items = prune._fetch_all(pages.__getitem__, "/x?s", key)
    assert [d["uid"] for d in items] == ["a", "b"]


# ---------------------------------------------------------------------------
# 実行部分（main）。Vercel への呼び出しは偽物に差し替える。
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_vercel(monkeypatch):
    """Vercel API の偽物。呼び出しを記録し、渡した一覧を返す。"""
    state = {"deployments": [], "aliases": [], "aliased_now": set(), "calls": []}

    def fake_call(method: str, path: str, token: str) -> dict:
        state["calls"].append((method, path))
        if path.startswith("/v6/deployments"):
            return {"deployments": state["deployments"]}
        if path.startswith("/v4/aliases"):
            return {"aliases": state["aliases"]}
        if path.startswith("/v2/deployments/"):
            uid = path.split("/")[3]
            return {"aliases": ["x.vercel.app"] if uid in state["aliased_now"] else []}
        return {}

    monkeypatch.setattr(prune, "_call", fake_call)
    monkeypatch.setattr(prune.time, "time", lambda: NOW / 1000)
    monkeypatch.setenv("VERCEL_TOKEN", "t")
    monkeypatch.setenv("VERCEL_PROJECT_ID", "prj")
    monkeypatch.setenv("VERCEL_ORG_ID", "team")
    return state


def _run(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(prune.sys, "argv", ["prune_deployments.py", *args])
    prune.main()


def _deletes(state) -> list[str]:
    return [path for method, path in state["calls"] if method == "DELETE"]


def _two_old_ones(state) -> None:
    state["deployments"] = [
        deployment("prod", NOW - 1 * HOUR, target="production"),
        deployment("old-a", NOW - 5 * HOUR, target="production"),
        deployment("old-b", NOW - 6 * HOUR, target="production"),
    ]
    state["aliases"] = [alias("pickle-shuffle.vercel.app", "prod")]


def test_nothing_is_removed_when_no_alias_can_be_read(fake_vercel, monkeypatch, capsys):
    """エイリアスが1つも取れなければ何もしない。取得の失敗で全部消えないように。"""
    fake_vercel["deployments"] = [deployment("a", NOW - 5 * HOUR), deployment("b", NOW - 6 * HOUR)]
    _run(monkeypatch)
    assert _deletes(fake_vercel) == []
    assert "何もしない" in capsys.readouterr().out


def test_a_dry_run_deletes_nothing(fake_vercel, monkeypatch, capsys):
    _two_old_ones(fake_vercel)
    _run(monkeypatch, "--dry-run")
    assert _deletes(fake_vercel) == []
    assert "消していない" in capsys.readouterr().out


def test_the_deletions_go_to_the_right_deployments(fake_vercel, monkeypatch):
    _two_old_ones(fake_vercel)
    _run(monkeypatch)
    assert _deletes(fake_vercel) == [
        "/v13/deployments/old-a?teamId=team",
        "/v13/deployments/old-b?teamId=team",
    ]


def test_a_deployment_found_aliased_just_before_deletion_is_skipped(fake_vercel, monkeypatch):
    """一覧のページの境目で取りこぼしても、消す直前に見てエイリアスがあれば残す。"""
    _two_old_ones(fake_vercel)
    fake_vercel["aliased_now"] = {"old-a"}
    _run(monkeypatch)
    assert _deletes(fake_vercel) == ["/v13/deployments/old-b?teamId=team"]
