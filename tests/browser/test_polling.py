"""メンバー用画面が、必要なときだけ速く聞きにいくこと。

**枠の消費でいちばん効くのがここ。** 12台が3時間見ていると、5秒ごとでは
25,920 回になる。試合中は変わらないので間引き、切り替わる瞬間だけ速くする。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.browser

PASSWORD = "ciあいことば"


def _make_match(page, server) -> str:
    """8人で1試合作って開始し、練習会のトークンを返す。"""
    page.fill("#password", PASSWORD)
    page.click("#unlock")
    page.wait_for_selector("#main:not(.hidden)", timeout=10000)
    # 名前は実行ごとに変える。DB はテスト間で共有していて、練習会名は重複できない。
    page.fill("#new-session-name", f"間引き確認{uuid4().hex[:6]}")
    page.click("#create-session")
    page.wait_for_url("**/manage.html?session=*", timeout=15000)
    page.wait_for_selector("body[data-ready]", timeout=15000)
    token = page.url.split("session=")[1]
    for i in range(8):
        page.fill("#new-nickname", f"q{i}-{uuid4().hex[:4]}")
        page.click("#add-member")
        page.wait_for_function(
            "(n) => document.querySelectorAll('#members-body tr').length === n",
            arg=i + 1,
            timeout=10000,
        )
    return token


def _count_polls(member, seconds: float) -> int:
    """その間に `current` を何回聞いたか。"""
    member.evaluate("() => { window.__polls = 0; }")
    member.wait_for_timeout(int(seconds * 1000))
    return member.evaluate("() => window.__polls")


def test_it_asks_less_while_a_match_is_running(page, server, watch):
    """試合中で時間に余裕があるうちは、問い合わせを間引くこと。"""
    token = _make_match(page, server)
    page.goto(f"{server}/overview.html?session={token}", wait_until="networkidle")
    page.wait_for_selector("body[data-ready]", timeout=15000)
    page.click("#next")
    page.wait_for_function(
        "() => document.querySelectorAll('.court .player').length >= 8", timeout=15000
    )

    guest = page.context.browser.new_context(viewport={"width": 390, "height": 780})
    try:
        member = watch(guest.new_page())
        member.add_init_script(
            """
            window.__polls = 0;
            const original = window.fetch;
            window.fetch = function (...args) {
              if (String(args[0]).includes('/current')) window.__polls++;
              return original.apply(this, args);
            };
            """
        )
        member.goto(f"{server}/member.html?session={token}", wait_until="networkidle")
        member.wait_for_selector("body[data-ready]", timeout=15000)

        # まだ始まっていない → 速く聞く（3秒ごと）。12秒で3回以上。
        before_start = _count_polls(member, 12)
        assert before_start >= 3, f"開始前なのに間引いている: {before_start}回/12秒"

        page.click("#start")
        member.wait_for_timeout(4000)

        # 試合中で残り7分 → 間引く（15秒ごと）。20秒で3回以下。
        during = _count_polls(member, 20)
        assert during <= 3, f"試合中なのに間引けていない: {during}回/20秒"
    finally:
        guest.close()


def test_the_overview_backs_off_when_left_alone(page, server):
    """置き忘れた全体表示画面が、一晩中問い合わせ続けないこと。

    2秒ごとのままだと一晩で4万回を超え、DB も起きっぱなしになる。
    何も変わらない時間が続いたら間隔を伸ばす。

    実時間で30分待てないので、判断そのものを確かめる。**その画面の上で読む**
    （モジュールの先頭がその画面の要素を触るので、別の画面では動かない）。
    """
    token = _make_match(page, server)
    page.goto(f"{server}/overview.html?session={token}", wait_until="networkidle")
    page.wait_for_selector("body[data-ready]", timeout=15000)

    decision = page.evaluate(
        """async () => {
            const m = await import('/overview.js');
            return {
              busy: m.chooseInterval(0),
              justUsed: m.chooseInterval(29 * 60 * 1000),
              abandoned: m.chooseInterval(31 * 60 * 1000),
            };
        }"""
    )
    assert decision["busy"] == 2000, decision
    assert decision["justUsed"] == 2000, "使っている間に伸ばしている"
    assert decision["abandoned"] == 60000, "置き忘れても伸びない"


def test_the_member_screen_decides_by_the_clock(page, server, watch):
    """メンバー用画面の間隔が、手元の時計だけで決まること。"""
    token = _make_match(page, server)
    guest = page.context.browser.new_context(viewport={"width": 390, "height": 780})
    try:
        member = watch(guest.new_page())
        member.goto(f"{server}/member.html?session={token}", wait_until="networkidle")
        member.wait_for_selector("body[data-ready]", timeout=15000)
        decision = member.evaluate(
            """async () => {
                const m = await import('/member.js');
                return {
                  waiting: m.chooseInterval('stopped', null, true),
                  running: m.chooseInterval('running', 300, true),
                  endgame: m.chooseInterval('running', 20, true),
                  unlimited: m.chooseInterval('running', null, false),
                };
            }"""
        )
    finally:
        guest.close()
    assert decision["waiting"] == 3000, "まだ決まっていないのに間引いている"
    assert decision["running"] == 15000, "試合中に間引けていない"
    assert decision["endgame"] == 3000, "終わりが近いのに間引いている"
    assert decision["unlimited"] == 15000, "無制限で急いでいる"
