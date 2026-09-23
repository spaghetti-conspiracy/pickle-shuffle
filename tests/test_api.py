"""API の一連のフロー。"""

from __future__ import annotations

from app.scheduler.domain import Gender, Level


def lineup(data: dict) -> list:
    """コートごとの出場者。マッチの「組み合わせ」だけを取り出す。

    名前やレベルは表示の都合で変わるので含めない。不変則12 が守るのは
    「誰と誰が同じ試合に入るか」であって、表示のされ方ではない。
    """
    return [
        (
            court["id"],
            [p["id"] for p in court["match"]["team_a"]],
            [p["id"] for p in court["match"]["team_b"]],
        )
        for court in data["courts"]
        if court["match"]
    ]


def create_session(client, name="練習会", court_count=2) -> dict:
    response = client.post(
        "/api/sessions", json={"name": name, "court_count": court_count}
    )
    assert response.status_code == 201, response.text
    return response.json()


def add_members(client, session_id: int, count: int, beginners: int = 0) -> list[dict]:
    members = []
    for i in range(count):
        response = client.post(
            f"/api/sessions/{session_id}/members",
            json={
                "nickname": f"m{i + 1}",
                "gender": Gender.MALE.value if i % 2 == 0 else Gender.FEMALE.value,
                "level": (
                    Level.BEGINNER.value if i < beginners else Level.PICKLEBALL.value
                ),
            },
        )
        assert response.status_code == 201, response.text
        members.append(response.json())
    return members


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_api_responses_are_not_cached(client):
    """表示画面は2秒ごとに読むので、古い応答を使われると困る。

    練習会が破棄されたことにも気づけなくなる。
    """
    for path in ("/api/health", "/api/sessions"):
        assert client.get(path).headers.get("cache-control") == "no-store"


def test_static_files_are_revalidated(client):
    """端末が古い JavaScript を使い続けないよう、毎回サーバに確認させる。

    ETag は付いているので、変わっていなければ 304 が返るだけで通信量は増えない。
    """
    for path in ("/",):
        response = client.get(path)
        assert response.headers.get("cache-control") == "no-cache", path
        assert response.headers.get("etag"), path


def test_session_is_created_with_its_courts(client):
    session = create_session(client, court_count=3)
    assert [c["name"] for c in session["courts"]] == ["コート1", "コート2", "コート3"]
    assert all(c["in_use"] for c in session["courts"])


def test_each_session_gets_its_own_seed(client, db):
    from app.models import PracticeSession

    create_session(client, "午前")
    create_session(client, "午後")
    seeds = {s.random_seed for s in db.query(PracticeSession).all()}
    assert len(seeds) == 2


def test_full_flow_generate_start_and_skip(client):
    session = create_session(client)
    add_members(client, session["token"], 13)

    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    assert current["round_status"] == "pending"
    assert [c["state"] for c in current["courts"]] == ["match", "match"]
    assert len(current["waiting"]) == 5

    first_round = current["round_id"]
    skipped = client.post(f"/api/rounds/{first_round}/reject").json()
    assert skipped["round_id"] is None, "不採用にしたら表示するマッチは無くなる"

    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    started = client.post(f"/api/rounds/{current['round_id']}/adopt").json()
    assert started["round_status"] == "adopted"

    stats = client.get(f"/api/sessions/{session['token']}/stats").json()
    assert stats["adopted_rounds"] == 1
    assert sum(stats["play_counts"].values()) == 8


def test_starting_the_same_round_twice_is_rejected(client):
    """別端末が先に開始していたら 409 を返し、ポーリングで追従させる。"""
    session = create_session(client)
    add_members(client, session["token"], 8)
    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    assert client.post(f"/api/rounds/{current['round_id']}/adopt").status_code == 200
    assert client.post(f"/api/rounds/{current['round_id']}/adopt").status_code == 409


def test_editing_members_does_not_disturb_the_displayed_card(client):
    """管理画面でメンバーを編集しても、表示中のマッチの組み合わせは変わらない。

    revision は表示すべき中身の指紋なので、待機者の顔ぶれが変われば変わる
    （仕様 l.39-40「全体表示画面とメンバー用画面の内容は自動的に同期する」）。
    不変則12 が禁じているのは組み合わせが動くことなので、そちらを検証する。
    """
    session = create_session(client)
    members = add_members(client, session["token"], 13)
    before = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()

    playing = {
        p["id"]
        for court in before["courts"]
        if court["match"]
        for team in (court["match"]["team_a"], court["match"]["team_b"])
        for p in team
    }
    benched = next(m for m in members if m["id"] not in playing)
    response = client.patch(f"/api/members/{benched['id']}", json={"status": "resting"})
    assert response.status_code == 200

    after = client.get(f"/api/sessions/{session['token']}/current").json()
    assert lineup(after) == lineup(before), "編集で組み合わせが動いてはいけない"
    assert after["round_id"] == before["round_id"]

    regenerated = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    assert regenerated["revision"] != before["revision"]
    assert benched["id"] not in {p["id"] for p in regenerated["waiting"]}
    assert benched["id"] in {p["id"] for p in regenerated["resting"]}


def test_a_member_who_starts_resting_mid_round_is_flagged(client):
    """出場中の人が休憩になったら、表示画面に注意を出せるようにする。"""
    session = create_session(client)
    members = add_members(client, session["token"], 13)
    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    playing = next(
        p["id"]
        for court in current["courts"]
        if court["match"]
        for p in court["match"]["team_a"]
    )
    client.patch(f"/api/members/{playing}", json={"status": "resting"})

    after = client.get(f"/api/sessions/{session['token']}/current").json()
    nickname = next(m["nickname"] for m in members if m["id"] == playing)
    assert after["stale_members"] == [nickname]


def test_a_level_change_mid_round_is_flagged(client):
    """出場中の人のレベルを変えたら、表示画面に注意を出す。

    表示中のマッチ自体は動かさない（不変則12）ので、
    食い違いは注意書きでしか伝えられない。
    """
    session = create_session(client)
    members = add_members(client, session["token"], 13)
    before = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    playing = next(
        p["id"]
        for court in before["courts"]
        if court["match"]
        for p in court["match"]["team_a"]
    )
    client.patch(f"/api/members/{playing}", json={"level": Level.BEGINNER.value})

    after = client.get(f"/api/sessions/{session['token']}/current").json()
    nickname = next(m["nickname"] for m in members if m["id"] == playing)
    assert after["stale_members"] == [nickname]
    assert after["revision"] != before["revision"], "注意書きが出たら描き直される"

    # 色分け用にレベルは今の値を返すので、組み合わせだけを比べる。
    assert lineup(after) == lineup(before), "マッチの組み合わせは動かさない"


def test_editing_someone_who_is_not_playing_is_not_flagged(client):
    """出ていない人を編集しても、注意は出さない。出すと読み上げの邪魔になる。"""
    session = create_session(client)
    add_members(client, session["token"], 13)
    before = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    waiting = before["waiting"][0]["id"]

    client.patch(f"/api/members/{waiting}", json={"level": Level.BEGINNER.value})

    after = client.get(f"/api/sessions/{session['token']}/current").json()
    assert after["stale_members"] == [], "出ていない人の編集で注意は出さない"
    assert lineup(after) == lineup(before), "組み合わせは動かない"


# ---------------------------------------------------------------------------
# コートの増減
# ---------------------------------------------------------------------------


def test_a_court_can_be_turned_into_a_practice_court(client):
    """初心者の育成用に1面を試合から外す。表示では練習コートとわかる。"""
    session = create_session(client, court_count=2)
    add_members(client, session["token"], 13)
    client.post(f"/api/sessions/{session['token']}/rounds/generate")

    court = session["courts"][1]
    response = client.patch(
        f"/api/sessions/{session['token']}/courts/{court['id']}", json={"in_use": False}
    )
    assert response.status_code == 200
    assert response.json()["in_use"] is False

    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    assert [c["state"] for c in current["courts"]] == ["match", "practice"]
    assert len(current["waiting"]) == 9

    client.patch(
        f"/api/sessions/{session['token']}/courts/{court['id']}", json={"in_use": True}
    )
    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    assert [c["state"] for c in current["courts"]] == ["match", "match"]


def test_an_unused_court_is_distinguished_from_a_practice_court(client):
    """人数が足りずに空くコートと、練習用に外したコートを区別して出す。"""
    session = create_session(client, court_count=2)
    add_members(client, session["token"], 6)
    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    assert [c["state"] for c in current["courts"]] == ["match", "idle"]


def test_courts_wait_before_the_first_generation(client):
    """まだ生成していないコートを「人数が足りません」と言わない。

    13名いるのに人数不足と出ると、メンバー登録やコート設定を疑わせてしまう。
    生成していないだけなので、押せば埋まると分かる状態にする。
    """
    session = create_session(client, court_count=2)
    add_members(client, session["token"], 13)

    current = client.get(f"/api/sessions/{session['token']}/current").json()
    assert current["round_id"] is None
    assert [c["state"] for c in current["courts"]] == ["waiting", "waiting"]


def test_courts_go_back_to_waiting_after_a_skip(client):
    """スキップして pending が無くなった直後も、人数不足とは言わない。"""
    session = create_session(client, court_count=2)
    add_members(client, session["token"], 13)
    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    after = client.post(f"/api/rounds/{current['round_id']}/reject").json()
    assert after["round_id"] is None
    assert [c["state"] for c in after["courts"]] == ["waiting", "waiting"]


def test_a_practice_court_is_not_called_short_of_players(client):
    """練習用に外したコートは、生成前でも「練習コート」と分かる。"""
    session = create_session(client, court_count=2)
    add_members(client, session["token"], 13)
    client.patch(
        f"/api/sessions/{session['token']}/courts/{session['courts'][1]['id']}",
        json={"in_use": False},
    )
    current = client.get(f"/api/sessions/{session['token']}/current").json()
    assert [c["state"] for c in current["courts"]] == ["waiting", "practice"]


def test_courts_can_be_renamed(client):
    session = create_session(client)
    court = session["courts"][0]
    response = client.patch(
        f"/api/sessions/{session['token']}/courts/{court['id']}", json={"name": "手前"}
    )
    assert response.json()["name"] == "手前"


def test_the_last_court_cannot_be_taken_out(client):
    session = create_session(client, court_count=2)
    first, second = session["courts"]
    client.patch(
        f"/api/sessions/{session['token']}/courts/{first['id']}", json={"in_use": False}
    )
    response = client.patch(
        f"/api/sessions/{session['token']}/courts/{second['id']}", json={"in_use": False}
    )
    assert response.status_code == 409


def test_court_names_reach_the_display(client):
    """コート名は会場との対応付けに使うので、表示側まで届く必要がある。

    画面の回転機能はやめて、コート名で会場と合わせる方針にした。
    """
    session = create_session(client, court_count=2)
    add_members(client, session["token"], 8)
    client.patch(
        f"/api/sessions/{session['token']}/courts/{session['courts'][0]['id']}",
        json={"name": "入口側"},
    )
    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    assert [c["name"] for c in current["courts"]] == ["入口側", "コート2"]


# ---------------------------------------------------------------------------
# メンバー用画面の QR コード
# ---------------------------------------------------------------------------


def test_member_qr_is_served_as_svg(client):
    """全体表示画面に出す QR。読み取るとメンバー用画面が開く。"""
    session = create_session(client)
    response = client.get(f"/api/sessions/{session['token']}/member-qr.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in response.text


def test_member_qr_is_a_standalone_svg(client):
    """``<img>`` から読み込むので、名前空間付きの独立した SVG 文書である必要がある。

    HTML への直接埋め込み用の出力（xmlns なし）だと画像として読めず、
    ブラウザでは QR が表示されない。
    """
    session = create_session(client)
    body = client.get(f"/api/sessions/{session['token']}/member-qr.svg").text
    assert 'xmlns="http://www.w3.org/2000/svg"' in body


def test_tokens_are_not_guessable_from_another_session(client):
    """メンバーは QR で自分の練習会の URL を知る。そこから他の練習会に行けてはいけない。

    認証は付けない方針なので、推測できない識別子であることが唯一の守りになる。
    """
    tokens = [create_session(client, f"会{i}")["token"] for i in range(5)]
    assert len(set(tokens)) == 5
    assert all(len(t) == 10 for t in tokens)
    # 連番でも、1文字違いでもない
    for a, b in zip(tokens[:-1], tokens[1:], strict=True):
        assert sum(1 for x, y in zip(a, b, strict=True) if x != y) >= 4
    assert client.get("/api/sessions/zzzzzzzzzz/current").status_code == 404


def test_a_near_miss_token_is_rejected(client):
    """1文字変えただけの URL では開けない。"""
    token = create_session(client)["token"]
    wrong = ("a" if token[0] != "a" else "b") + token[1:]
    assert client.get(f"/api/sessions/{wrong}/current").status_code == 404


def test_the_internal_id_is_not_exposed(client):
    """連番の内部 id は外に出さない。"""
    session = create_session(client)
    assert "id" not in session
    assert set(session) == {
        "token",
        "name",
        "created_at",
        "courts",
        "highlight_beginners",
    }


def test_member_qr_points_at_the_host_the_browser_used(client):
    """QR の URL は、ブラウザが実際に叩いたホストから組み立てる。

    手元の LAN の IP でも Vercel のドメインでも、そのまま読み取れるようにするため。
    """
    from app.api import member_page_url

    class _Request:
        base_url = "http://192.168.1.10:8000/"

    assert member_page_url(_Request(), "abc123xyz0") == (
        "http://192.168.1.10:8000/member.html?session=abc123xyz0"
    )


def test_public_base_url_overrides_the_request_host(monkeypatch):
    """サーバと同じ PC で localhost として開くと、QR がスマホから届かなくなる。

    そのために PUBLIC_BASE_URL で上書きできるようにしてある。
    """
    from dataclasses import replace

    from app import api

    class _Request:
        base_url = "http://localhost:8000/"

    monkeypatch.setattr(
        api, "settings", replace(api.settings, public_base_url="http://192.168.1.10:8000")
    )
    assert api.member_page_url(_Request(), "tok1234567") == (
        "http://192.168.1.10:8000/member.html?session=tok1234567"
    )


def test_current_reports_the_member_url(client):
    """全体表示画面が QR と同じ URL を文字でも出せるように返す。"""
    session = create_session(client)
    current = client.get(f"/api/sessions/{session['token']}/current").json()
    assert current["member_url"].endswith(f"/member.html?session={session['token']}")


def test_member_qr_for_a_missing_session(client):
    assert client.get("/api/sessions/999/member-qr.svg").status_code == 404


# ---------------------------------------------------------------------------
# メンバーの辞書
# ---------------------------------------------------------------------------


def test_attributes_are_reused_across_sessions(client):
    """ニックネームをキーに属性を引き当てる。統計は共有しない。"""
    morning = create_session(client, "午前")
    client.post(
        f"/api/sessions/{morning['token']}/members",
        json={"nickname": "たろう", "gender": "male", "level": "beginner"},
    )
    profiles = client.get("/api/member-profiles").json()
    assert {"nickname": "たろう", "gender": "male", "level": "beginner"} in profiles


def test_statistics_are_never_shared_between_sessions(client):
    """同じニックネームでも、別の練習会に参加回数を持ち込まない。"""
    morning = create_session(client, "午前")
    add_members(client, morning["token"], 8)
    current = client.post(f"/api/sessions/{morning['token']}/rounds/generate").json()
    client.post(f"/api/rounds/{current['round_id']}/adopt")
    assert sum(
        client.get(f"/api/sessions/{morning['token']}/stats").json()["play_counts"].values()
    ) == 8

    afternoon = create_session(client, "午後")
    add_members(client, afternoon["token"], 8)
    stats = client.get(f"/api/sessions/{afternoon['token']}/stats").json()
    assert stats["adopted_rounds"] == 0
    assert stats["play_counts"] == {}


def test_the_dictionary_is_updated_last_write_wins(client):
    session = create_session(client)
    client.post(
        f"/api/sessions/{session['token']}/members",
        json={"nickname": "はな", "gender": "female", "level": "beginner"},
    )
    client.post(
        f"/api/sessions/{session['token']}/members",
        json={"nickname": "はな", "gender": "female", "level": "pickleball"},
    )
    profiles = {p["nickname"]: p for p in client.get("/api/member-profiles").json()}
    assert profiles["はな"]["level"] == "pickleball"


def test_the_dictionary_survives_deleting_a_session(client):
    session = create_session(client)
    client.post(
        f"/api/sessions/{session['token']}/members",
        json={"nickname": "のこる", "gender": "other", "level": "pickleball"},
    )
    assert client.delete(f"/api/sessions/{session['token']}").status_code == 204
    assert client.get(f"/api/sessions/{session['token']}").status_code == 404
    assert any(p["nickname"] == "のこる" for p in client.get("/api/member-profiles").json())


def test_duplicate_nicknames_are_allowed_but_reported(client):
    """同名は禁止しない。どちらか分からなくなるので警告だけ出す。"""
    session = create_session(client)
    for _ in range(2):
        client.post(
            f"/api/sessions/{session['token']}/members",
            json={"nickname": "ゆうき", "gender": "male", "level": "pickleball"},
        )
    add_members(client, session["token"], 6)

    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    assert current["duplicate_nicknames"] == ["ゆうき"]


def test_a_member_who_never_played_is_deleted_outright(client):
    session = create_session(client)
    members = add_members(client, session["token"], 8)
    assert client.delete(f"/api/members/{members[0]['id']}").status_code == 204
    remaining = client.get(f"/api/sessions/{session['token']}/members").json()
    assert len(remaining) == 7


def test_a_member_with_history_is_kept_as_left(client):
    session = create_session(client)
    members = add_members(client, session["token"], 8)
    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    client.post(f"/api/rounds/{current['round_id']}/adopt")

    client.delete(f"/api/members/{members[0]['id']}")
    listed = client.get(f"/api/sessions/{session['token']}/members").json()
    assert next(m for m in listed if m["id"] == members[0]["id"])["status"] == "left"


def test_undo_takes_back_the_last_start(client):
    session = create_session(client)
    add_members(client, session["token"], 13)
    current = client.post(f"/api/sessions/{session['token']}/rounds/generate").json()
    client.post(f"/api/rounds/{current['round_id']}/adopt")
    assert client.get(f"/api/sessions/{session['token']}/stats").json()["adopted_rounds"] == 1

    client.post(f"/api/rounds/{current['round_id']}/undo")
    stats = client.get(f"/api/sessions/{session['token']}/stats").json()
    assert stats["adopted_rounds"] == 0
    assert stats["play_counts"] == {}


def test_generating_without_enough_players(client):
    session = create_session(client)
    add_members(client, session["token"], 3)
    response = client.post(f"/api/sessions/{session['token']}/rounds/generate")
    assert response.status_code == 409
    assert "4人以上" in response.json()["detail"]
