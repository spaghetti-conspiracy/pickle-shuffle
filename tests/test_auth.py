"""合言葉の門。

**いたずら防止であって、秘密を守る仕組みではない。** 練習会のトークンを
持っている人（QR を読んだメンバー、管理画面を開いている人）は今までどおり
通れる。門をかけるのは、トークンを持たなくても叩ける入口だけ。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.auth import COOKIE_NAME, hash_password, issue_cookie, verify_password
from app.config import settings
from app.errors import UnauthorizedError
from app.scheduler.domain import Gender, Level
from tests.test_api import add_members, create_session

GATED = [
    ("get", "/api/sessions"),
    ("post", "/api/sessions"),
    ("get", "/api/people"),
    ("post", "/api/people"),
]
"""合言葉が要る入口の代表。実際はメンバー用画面が使う read 以外すべて。"""


def test_the_password_is_not_stored_in_the_clear():
    stored = hash_password("ひみつ")
    assert "ひみつ" not in stored
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("ひみつ", stored)
    assert not verify_password("ちがう", stored)


def test_a_wrong_password_is_refused(guest_client):
    response = guest_client.post("/api/login", json={"password": "ちがう"})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_the_right_password_opens_the_gate(guest_client):
    assert guest_client.get("/api/sessions").status_code == 401
    response = guest_client.post(
        "/api/login", json={"password": settings.admin_password}
    )
    assert response.status_code == 204
    assert COOKIE_NAME in response.cookies
    assert guest_client.get("/api/sessions").status_code == 200


def test_every_gated_entrance_is_closed(guest_client):
    """合言葉を持たない人が、練習会や台帳を触れないこと。"""
    for method, path in GATED:
        call = getattr(guest_client, method)
        response = call(path) if method == "get" else call(path, json={})
        assert response.status_code == 401, f"{method} {path} が素通りしている"


def test_logging_out_closes_the_gate_again(client):
    assert client.get("/api/sessions").status_code == 200
    assert client.post("/api/logout").status_code == 204
    assert client.get("/api/sessions").status_code == 401


def test_a_forged_cookie_does_not_pass(guest_client):
    """中身を作っただけのクッキーでは通らない。"""
    guest_client.cookies.set(COOKIE_NAME, "1:" + "0" * 64)
    assert guest_client.get("/api/sessions").status_code == 401


def test_changing_the_password_invalidates_old_cookies(guest_client, db):
    """パスワードを変えたら、前のクッキーは通らない。

    クッキーの署名鍵がパスワードのハッシュなので、DB を作り直さなくても
    古い端末が締め出される。
    """
    from sqlalchemy import select

    from app.models import Admin

    guest_client.post("/api/login", json={"password": settings.admin_password})
    assert guest_client.get("/api/sessions").status_code == 200

    admin = db.scalars(select(Admin)).one()
    admin.password_hash = hash_password("あたらしい合言葉")
    db.commit()

    assert guest_client.get("/api/sessions").status_code == 401


def test_the_cookie_belongs_to_one_admin(db):
    """別の管理者の署名では通らない。"""
    from sqlalchemy import select

    from app.models import Admin

    admin = db.scalars(select(Admin)).one()
    assert issue_cookie(admin.id, admin.password_hash) != issue_cookie(
        admin.id + 1, admin.password_hash
    )


# ---------------------------------------------------------------------------
# 門をかけない入口
# ---------------------------------------------------------------------------


def test_the_member_screen_needs_no_password(client, guest_client):
    """QR を読んだメンバーは合言葉なしで見られる。

    ここに門をかけると、練習会のたびに全員へ合言葉を配ることになる。
    メンバー用画面が使うのはこの1本だけなので、開けるのもこれだけでよい。
    """
    session = create_session(client)
    add_members(client, session["token"], 8)
    client.post(f"/api/sessions/{session['token']}/rounds/generate")

    response = guest_client.get(f"/api/sessions/{session['token']}/current")
    assert response.status_code == 200
    assert len(response.json()["courts"]) == 2


def test_everything_else_is_behind_the_gate(client, guest_client):
    """管理画面と全体表示画面が使う入口は、合言葉なしでは通らない。

    見えるのに押しても動かない画面を作らないため、表示だけの read も含めて
    閉めてある（画面側はトップへ戻す）。
    """
    session = create_session(client)
    add_members(client, session["token"], 8)
    pending = client.post(
        f"/api/sessions/{session['token']}/rounds/generate"
    ).json()

    token = session["token"]
    closed = [
        ("get", f"/api/sessions/{token}"),
        ("get", f"/api/sessions/{token}/members"),
        ("get", f"/api/sessions/{token}/stats"),
        ("get", f"/api/sessions/{token}/member-qr.svg"),
        ("post", f"/api/sessions/{token}/rounds/generate"),
        ("post", f"/api/rounds/{pending['round_id']}/adopt"),
        ("post", f"/api/rounds/{pending['round_id']}/timer/pause"),
    ]
    for method, path in closed:
        call = getattr(guest_client, method)
        response = call(path) if method == "get" else call(path, json={})
        assert response.status_code == 401, f"{method} {path} が素通りしている"


def test_a_new_endpoint_is_closed_by_default(client):
    """ルータを分けてあるので、足した API は既定で守られる。

    公開してよいものだけ `public_router` に置く、という形を崩さないための番人。
    """
    from app.api import public_router

    opened = {(list(route.methods)[0], route.path) for route in public_router.routes}
    assert opened == {
        ("GET", "/api/health"),
        ("POST", "/api/login"),
        ("POST", "/api/logout"),
        ("GET", "/api/sessions/{session_token}/current"),
    }, "公開する入口が増えている。メンバー用画面に本当に必要か確かめること"


# ---------------------------------------------------------------------------
# レビューで見つかった欠陥の回帰テスト
# ---------------------------------------------------------------------------


BROKEN_COOKIES = [
    ("桁あふれ", "9" * 30 + ":x"),
    ("区切りが無い", "1"),
    ("id が数字でない", "abc:def"),
    ("負の値", "-1:x"),
    ("長すぎる", "1:" + "a" * 500),
    ("空", ""),
]
"""HTTP で実際に送れる細工。ヘッダは ASCII なので、非 ASCII は関数で直接確かめる。"""


def test_a_broken_cookie_is_refused_not_crashed(guest_client):
    """細工されたクッキーで 500 にしない。

    合言葉を持たない相手が自由に送れる入口なので、桁あふれや非 ASCII で
    サーバが落ちるようでは門の意味が薄れる。

    クライアントの cookie jar を通さず、ヘッダを直接組み立てる。
    ブラウザが送らない値でも、HTTP としては送れてしまうため。
    """
    for label, value in BROKEN_COOKIES:
        response = guest_client.get(
            "/api/sessions", headers={"Cookie": f"{COOKIE_NAME}={value}"}
        )
        assert response.status_code == 401, f"{label}: {response.status_code}"


def test_a_non_ascii_cookie_is_refused_not_crashed():
    """非 ASCII を混ぜたクッキーでも例外にしない。

    ヘッダはバイト列なので、サーバ側には ASCII 外の文字を含む str として
    届き得る。`hmac.compare_digest` は str のままだと例外を投げる。
    """
    from app.auth import cookie_matches, read_cookie

    assert read_cookie("1:あいう") is None
    assert cookie_matches("1:あいう", 1, hash_password("x")) is False


def test_another_owners_rows_are_not_reachable_by_id(client, db):
    """よその団体の参加者・マッチは、連番の id を知っていても触れない。"""
    from app.models import Owner
    from app.services import people as people_service
    from app.services import rounds as rounds_service
    from app.services import sessions as sessions_service

    other = Owner(name="よその団体")
    db.add(other)
    db.commit()
    stranger_session = sessions_service.create_session(db, other, "よその練習会", 2)
    for i in range(8):
        sessions_service.add_member(
            db,
            stranger_session,
            people_service.add_person(
                db, other, nickname=f"x{i}", gender=Gender.MALE, level=Level.PICKLEBALL
            ),
        )
    stranger_member = sessions_service.list_members(db, stranger_session.id)[0]
    stranger_round = rounds_service.generate(db, stranger_session)

    assert client.patch(
        f"/api/members/{stranger_member.id}", json={"level": "beginner"}
    ).status_code == 404
    assert client.delete(f"/api/members/{stranger_member.id}").status_code == 404
    assert client.post(f"/api/rounds/{stranger_round.id}/adopt").status_code == 404


def test_changing_the_password_takes_effect_without_touching_the_db(db, monkeypatch):
    """`ADMIN_PASSWORD` を変えたら、次のログインから新しい合言葉で入れること。

    起動のたびに確かめると pbkdf2 を20万回まわすので、冷えた1回目が遅くなる。
    **合わなかったときだけ**見れば、ふだんの費用はゼロで済む。
    """
    from sqlalchemy import select

    import app.services.owners as owners
    from app.models import Admin

    before = db.scalars(select(Admin)).one().password_hash

    # Settings は frozen なので、差し替えた写しを置く。
    monkeypatch.setattr(
        owners, "settings", replace(owners.settings, admin_password="あたらしい合言葉")
    )

    with pytest.raises(UnauthorizedError):
        owners.authenticate(db, "まったく違う")

    admin = owners.authenticate(db, "あたらしい合言葉")
    assert admin.is_bootstrap is True
    assert admin.password_hash != before, "作り直されていない"

    # 元の合言葉はもう通らない。
    with pytest.raises(UnauthorizedError):
        owners.authenticate(db, settings.admin_password)


def test_a_real_admin_is_not_overwritten(db, monkeypatch):
    """本物の管理者を登録したあとの行は、環境変数で書き換えない。"""

    import app.services.owners as owners
    from app.models import Admin

    real = Admin(login="honmono", password_hash=hash_password("本物の合言葉"))
    db.add(real)
    db.commit()
    kept = real.password_hash

    monkeypatch.setattr(
        owners, "settings", replace(owners.settings, admin_password="別の合言葉")
    )
    owners.authenticate(db, "別の合言葉")  # bootstrap の行が作り直される

    db.refresh(real)
    assert real.password_hash == kept, "本物の管理者を書き換えている"
    assert owners.authenticate(db, "本物の合言葉").login == "honmono"
