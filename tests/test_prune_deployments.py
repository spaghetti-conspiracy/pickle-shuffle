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


def deployment(uid: str, state: str = "READY", target: str | None = None) -> dict:
    return {"uid": uid, "readyState": state, "target": target, "url": f"{uid}.vercel.app"}


def test_what_an_external_url_points_to_is_kept_even_if_it_is_old():
    """ロールバック直後のように、外向けの URL が古い方を指していればそちらを残す。

    「環境ごとに最新の1つ」で選ぶと、使われているデプロイを消してしまう。
    """
    deployments = [
        deployment("new-prod", target="production"),  # 新しいが、どこからも指されていない
        deployment("old-prod", target="production"),  # 本番の URL が指している
        deployment("staging"),
    ]
    aliases = [
        {"alias": "pickle-shuffle.vercel.app", "deploymentId": "old-prod"},
        {"alias": "pickle-shuffle-staging.vercel.app", "deploymentId": "staging"},
    ]
    keep, doomed = prune.choose(deployments, aliases)
    assert keep == {
        "old-prod": ["pickle-shuffle.vercel.app"],
        "staging": ["pickle-shuffle-staging.vercel.app"],
    }
    assert [d["uid"] for d in doomed] == ["new-prod"]


def test_failed_and_unreferenced_deployments_are_removed():
    deployments = [deployment("live"), deployment("old"), deployment("broken", state="ERROR")]
    aliases = [{"alias": "pickle-shuffle.vercel.app", "deploymentId": "live"}]
    _keep, doomed = prune.choose(deployments, aliases)
    assert sorted(d["uid"] for d in doomed) == ["broken", "old"]


@pytest.mark.parametrize("state", ["QUEUED", "INITIALIZING", "BUILDING"])
def test_a_deployment_still_in_progress_is_kept(state):
    """リリースの途中で、まだエイリアスが付く前のものを消さない。"""
    deployments = [deployment("live"), deployment("releasing", state=state)]
    aliases = [{"alias": "pickle-shuffle.vercel.app", "deploymentId": "live"}]
    keep, doomed = prune.choose(deployments, aliases)
    assert "releasing" in keep
    assert doomed == []


def test_an_alias_with_a_nested_deployment_is_understood():
    """エイリアスが deployment を入れ子で持つ形でも、指している先を読める。"""
    deployments = [deployment("live"), deployment("old")]
    aliases = [{"alias": "pickle-shuffle.vercel.app", "deployment": {"id": "live"}}]
    keep, doomed = prune.choose(deployments, aliases)
    assert list(keep) == ["live"]
    assert [d["uid"] for d in doomed] == ["old"]


def test_one_deployment_can_be_kept_for_several_urls():
    deployments = [deployment("live")]
    aliases = [
        {"alias": "pickle-shuffle.vercel.app", "deploymentId": "live"},
        {"alias": "pickle.example.com", "deploymentId": "live"},
    ]
    keep, _doomed = prune.choose(deployments, aliases)
    assert keep == {"live": ["pickle-shuffle.vercel.app", "pickle.example.com"]}


def test_every_page_of_a_listing_is_fetched():
    """100件を超えても、ページ送りをたどって全部取る。"""
    pages = {
        "/v6/deployments?x&limit=100": {"deployments": [{"uid": "a"}], "pagination": {"next": 5}},
        "/v6/deployments?x&limit=100&until=5": {
            "deployments": [{"uid": "b"}],
            "pagination": {"next": None},
        },
    }
    items = prune._fetch_all(pages.__getitem__, "/v6/deployments?x", "deployments")
    assert [d["uid"] for d in items] == ["a", "b"]


def test_nothing_is_removed_when_no_alias_can_be_read(monkeypatch, capsys):
    """エイリアスが1つも取れなければ何もしない。取得の失敗で全部消えないように。"""
    calls: list[tuple[str, str]] = []

    def fake_call(method: str, path: str, token: str) -> dict:
        calls.append((method, path))
        if path.startswith("/v6/deployments"):
            return {"deployments": [deployment("a"), deployment("b")]}
        return {"aliases": []}

    monkeypatch.setattr(prune, "_call", fake_call)
    monkeypatch.setenv("VERCEL_TOKEN", "t")
    monkeypatch.setenv("VERCEL_PROJECT_ID", "prj")
    monkeypatch.setenv("VERCEL_ORG_ID", "team")
    monkeypatch.setattr(prune.sys, "argv", ["prune_deployments.py"])

    prune.main()

    assert all(method == "GET" for method, _path in calls), "何か消そうとした"
    assert "何もしない" in capsys.readouterr().out
