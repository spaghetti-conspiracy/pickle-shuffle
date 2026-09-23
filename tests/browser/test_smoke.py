"""主要な径路だけのブラウザテスト。

**壊れたら痛いところ**に絞ってある。網羅ではなく、
「画面が開かない」「押しても何も起きない」を push 前に捕まえるのが目的。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

PASSWORD = "ciあいことば"


def _unique(prefix: str) -> str:
    """このテストだけの名前。

    DB はテスト間で共有している（起動が重いので session スコープ）。
    固定の名前を使うと、**実行順や本数が変わっただけで落ちる**テストになる。
    """
    return f"{prefix}{uuid4().hex[:6]}"


def _unlock(page) -> None:
    page.fill("#password", PASSWORD)
    page.click("#unlock")
    page.wait_for_selector("#main:not(.hidden)", timeout=10000)


def _row_with(page, nickname: str):
    """名前で行を引く。

    **番号では指さない。** 一覧は名前順に並ぶので、改名や削除で描き直されると
    番号が別の行を指す（そのまま押すと、消すつもりのない人を消す）。
    """
    return page.locator(f'#people-body tr[data-nickname="{nickname}"]')


def _nicknames(page) -> list[str]:
    return page.eval_on_selector_all(
        "#people-body tr td:first-child input", "els => els.map(e => e.value)"
    )


def _make_session(page, server, name: str) -> str:
    _unlock(page)
    page.fill("#new-session-name", name)
    page.click("#create-session")
    page.wait_for_url("**/manage.html?session=*", timeout=15000)
    page.wait_for_selector("body[data-ready]", timeout=15000)
    return page.url.split("session=")[1]


def test_the_password_guards_the_top_screen(page):
    """合言葉を間違えたら開かない。正しければ開く。"""
    assert page.locator("#gate").is_visible()
    assert not page.locator("#main").is_visible()

    page.fill("#password", "ちがう")
    page.click("#unlock")
    page.wait_for_function(
        "() => document.getElementById('gate-error').textContent", timeout=10000
    )

    _unlock(page)
    assert page.locator("#main").is_visible()


def test_the_register_can_be_managed(page, server):
    """名簿の追加・改名・削除。練習会と関係なく触れること。"""
    _unlock(page)
    page.click("#open-members")
    page.wait_for_url("**/members.html", timeout=10000)
    page.wait_for_selector("body[data-ready]", timeout=15000)

    assert page.locator("#import-event, #import-members").count() == 0, "取り込みの導線がある"

    base = _unique("たろう")
    before = len(_nicknames(page))
    for _ in range(2):
        page.fill("#new-nickname", base)
        page.click("#add-person")
        before += 1
        page.wait_for_function(
            "(n) => document.querySelectorAll('#people-body tr').length === n",
            arg=before,
            timeout=10000,
        )
    mine = [name for name in _nicknames(page) if name.startswith(base)]
    assert mine == [base, base + "2"], f"番号が付いていない: {mine}"

    renamed = _unique("たろちゃん")
    name_input = _row_with(page, base).locator("input").first
    name_input.fill(renamed)
    name_input.press("Enter")
    expect(_row_with(page, renamed)).to_have_count(1, timeout=10000)

    doomed = _row_with(page, base + "2")
    doomed.wait_for(timeout=10000)
    page.once("dialog", lambda dialog: dialog.accept())
    doomed.locator("button.danger").click()
    expect(_row_with(page, base + "2")).to_have_count(0, timeout=10000)
    assert renamed in _nicknames(page), "改名した人まで消えている"


def test_a_participant_is_added_and_removed(page, server):
    """台帳から参加者にして、「外す」と名簿に残ること。"""
    _make_session(page, server, _unique("通し確認"))
    nickname = _unique("はなこ")

    page.fill("#new-nickname", nickname)
    page.click("#add-member")
    page.wait_for_function(
        "() => document.querySelectorAll('#members-body tr').length === 1", timeout=10000
    )

    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#members-body tr").first.locator("button.danger").click()
    page.wait_for_function(
        "() => document.querySelectorAll('#members-body tr').length === 0", timeout=10000
    )

    page.goto(f"{server}/members.html", wait_until="networkidle")
    page.wait_for_selector("body[data-ready]", timeout=15000)
    assert nickname in _nicknames(page), "外したら名簿からも消えている"


def test_a_match_is_generated_and_started(page, server, watch):
    """マッチが組め、開始でき、メンバー用画面が合言葉なしで見られること。"""
    token = _make_session(page, server, _unique("マッチ確認"))
    for i in range(8):
        page.fill("#new-nickname", _unique(f"p{i}-"))
        page.click("#add-member")
        # **行が増えるまで待つ。** 固定待ちだと、最後の追加が飛ぶ前に
        # 画面を移ってしまい、7人でマッチが組めなくなることがある。
        page.wait_for_function(
            "(n) => document.querySelectorAll('#members-body tr').length === n",
            arg=i + 1,
            timeout=10000,
        )

    page.goto(f"{server}/overview.html?session={token}", wait_until="networkidle")
    page.wait_for_selector("body[data-ready]", timeout=15000)
    page.click("#next")
    page.wait_for_function(
        "() => document.querySelectorAll('.court .player').length >= 8", timeout=15000
    )
    page.click("#start")
    # 開始したら「次のマッチ」は引っ込む（読み上げ中に組み合わせが変わらないように）。
    page.wait_for_function(
        "() => document.getElementById('next').classList.contains('hidden')",
        timeout=15000,
    )

    # メンバー用画面は合言葉なしで開ける（QR を読んだ人のため）。
    guest = page.context.browser.new_context(viewport={"width": 390, "height": 780})
    try:
        member = watch(guest.new_page())
        member.goto(f"{server}/member.html?session={token}", wait_until="networkidle")
        member.wait_for_selector("body[data-ready]", timeout=15000)
        assert "合言葉" not in member.inner_text("body")
        member.wait_for_function(
            "() => document.querySelectorAll('.player').length >= 4", timeout=15000
        )
    finally:
        guest.close()


def test_it_says_what_it_is_doing_while_slow(page):
    """待たせるときだけ、何をしているかを見せること。

    サーバーレスなので、しばらく使っていないと最初の1回は関数の起動と
    DB の点検で数秒かかる。黙って止まって見えると壊れたと思われる。
    速いときは何も出さない。
    """
    result = page.evaluate(
        """async () => {
            const m = await import('/api.js');
            const box = document.getElementById('gate-error');

            // 速く終わったときは何も出ない。
            const quick = m.showWhileSlow(box, 'テスト', { after: 300 });
            await new Promise((r) => setTimeout(r, 50));
            const whenQuick = box.textContent;
            quick();

            // 待たされると出て、点が増えて動いていることが分かる。
            const slow = m.showWhileSlow(box, '点検しています', { after: 50 });
            await new Promise((r) => setTimeout(r, 200));
            const first = box.textContent;
            await new Promise((r) => setTimeout(r, 500));
            const later = box.textContent;
            slow();
            return { whenQuick, first, later, afterDone: box.textContent };
        }"""
    )
    assert result["whenQuick"] == "", "速いのに出ている"
    assert result["first"].startswith("点検しています"), result
    assert result["later"] != result["first"], "止まって見える（点が増えていない）"
    assert result["afterDone"] == "", "終わったのに残っている"
