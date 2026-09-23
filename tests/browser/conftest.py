"""ブラウザテスト用の足場。

**ふだんの `pytest` では走らない**（`browser` マーカーで外してある）。
Playwright を入れていない手元でも、ユニットテストはそのまま通る。

    uv run pytest -m browser        # 走らせるとき
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory) -> str:
    """テスト専用の DB でアプリを起動し、その URL を返す。

    本物のサーバに当てる。画面まわりの壊れ方は、静的な検査では見つからない。
    """
    port = _free_port()
    db = tmp_path_factory.mktemp("browser") / "app.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db}",
        "ADMIN_PASSWORD": "ciあいことば",
        "PYTHONPATH": str(ROOT),
    }
    # 起動に失敗したとき、理由が残らないと CI で手が出せない。
    log = tmp_path_factory.mktemp("browser-log") / "uvicorn.log"
    with log.open("w") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError(f"サーバが落ちた:\n{log.read_text()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.5)
            else:
                raise RuntimeError(f"サーバが応答しない:\n{log.read_text()}")
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


@pytest.fixture
def browser():
    """ブラウザ本体。**入っていなければ skip せずに落とす。**

    `importorskip` にすると、CI で playwright が入らなくなった日に
    「0件実行で緑」になり、release の前提が黙って空振りする。
    """
    with sync_playwright() as playwright:
        opened = playwright.chromium.launch(args=["--no-sandbox"])
        try:
            yield opened
        finally:
            opened.close()


@pytest.fixture
def errors() -> list[str]:
    """画面で出た例外。開いたページすべてから集める。"""
    return []


@pytest.fixture
def watch(errors):
    """ページの例外を拾うようにする。メンバー用画面など、あとから開く窓にも使う。"""

    def attach(target):
        target.on("pageerror", lambda error: errors.append(str(error)))
        return target

    return attach


@pytest.fixture
def page(browser, server, watch, errors):
    """合言葉を通していないブラウザのページ。"""
    context = browser.new_context(viewport={"width": 1180, "height": 900})
    opened = watch(context.new_page())
    opened.goto(server, wait_until="networkidle")
    opened.wait_for_selector("body[data-ready]", timeout=15000)
    try:
        yield opened
    finally:
        context.close()
    assert not errors, f"画面で例外が出ている: {errors}"
