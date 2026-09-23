"""tennisbear のイベントページの解析と、練習会への取り込み。

ネットワークには触らない。解析は保存したページで確かめる。

フィクスチャは**実際のイベントページから作った**ものに、氏名・ユーザID・画像URLだけ
差し替えを入れてある。埋め込まれた状態の形（短縮変数を含む）と、レベル・性別の
分布は実物のまま。実際にどういう顔ぶれだったか分かっているので、期待値に使える。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from app.errors import UpstreamError, ValidationError
from app.scheduler.domain import Gender, Level, MemberStatus
from app.services import sessions as sessions_service
from app.tennisbear import (
    Participant,
    level_from_tennisbear,
    parse_event_page,
)

FIXTURES = Path(__file__).parent / "fixtures"

#: 終了したイベント。顔ぶれと結果が分かっているので期待値に使える。
FINISHED = FIXTURES / "tennisbear_event.html"

#: 開催前のイベント。終了後とページの作りが違う可能性があるので別に押さえる。
UPCOMING = FIXTURES / "tennisbear_event_upcoming.html"


def sample_html() -> str:
    return FINISHED.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# レベルの対応
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tennis", "pickleball", "expected"),
    [
        (1, 1, Level.BEGINNER),  # どちらも はじめて
        (2, 1, Level.BEGINNER),  # テニスも初心者どまり
        (3, 1, Level.RACKET_EXPERIENCED),  # ラケットは振れる
        (6, 1, Level.RACKET_EXPERIENCED),
        (1, 2, Level.PICKLEBALL),  # ピックルボールを始めていれば経験者
        (1, 5, Level.PICKLEBALL),
        (6, 5, Level.PICKLEBALL),
    ],
)
def test_levels_map_to_the_apps_three_steps(tennis, pickleball, expected):
    """2軸のレベルを、このアプリの3段階に落とす。

    「ピックルボール未経験かつラケット未経験」だけが初心者。ここを混ぜると
    仕様3a（初心者同士を組ませない）の避け方が変わってしまう。
    """
    assert level_from_tennisbear(tennis, pickleball) is expected


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parses_every_participant():
    """参加者を取りこぼさず、キャンセル待ちを混ぜない。

    この回は18名だった。ページには「キャンセル待ち」や「ブックマークした
    プレイヤー」の欄もあるので、参加者だけを拾えているかを人数で見る。
    """
    people = parse_event_page(sample_html())
    assert len(people) == 18
    assert len({p.user_id for p in people}) == 18, "同じ人を2回拾っている"
    assert all(p.nickname for p in people), "名前を取り出せていない人がいる"


def test_reads_gender_and_level_as_they_were():
    """実際の顔ぶれと同じ内訳になる。

    この回は 初心者1・ラケット経験者2・ピックルボール経験者15、
    男性12・女性6 だった。
    """
    people = parse_event_page(sample_html())
    levels = Counter(p.level for p in people)
    assert levels[Level.BEGINNER] == 1
    assert levels[Level.RACKET_EXPERIENCED] == 2
    assert levels[Level.PICKLEBALL] == 15

    genders = Counter(p.gender for p in people)
    assert genders[Gender.MALE] == 12
    assert genders[Gender.FEMALE] == 6


def test_an_event_before_its_day_parses_too():
    """開催前のイベントでも読める。

    終了後のページだけで確かめると、当日までしか出ない欄（キャンセル待ちなど）が
    あったときに気づけない。開催前のページも別に押さえておく。
    """
    people = parse_event_page(UPCOMING.read_text(encoding="utf-8"))
    assert len(people) == 14
    assert len({p.user_id for p in people}) == 14
    assert all(p.nickname for p in people)
    # この回は全員がピックルボール経験者だった
    assert {p.level for p in people} == {Level.PICKLEBALL}


def test_the_estimate_misses_someone_it_cannot_know_about():
    """推定が外れる実例を残しておく。

    この回の「参加者7」は、実際にはラケットスポーツ未経験だった。
    しかし tennisbear のテニスレベルは「初中級」で登録されているので、
    **どんな規則を書いてもここから未経験だとは分からない**。

    取り込みはあくまで下書きで、管理者が直す前提。ここを忘れて
    「取り込めば正しい」と思うと、初心者同士のペアができてしまう。
    """
    people = {p.nickname: p for p in parse_event_page(sample_html())}
    estimated = people["参加者7"]
    assert estimated.level is Level.RACKET_EXPERIENCED, "推定はラケット経験者になる"
    # 実際は Level.BEGINNER だった。取り込み後に管理画面で直す。


def test_level_is_not_silently_defaulted():
    """レベルを読めずに既定値へ落ちていないか。

    読めないと全員が初心者側に倒れる。そうなっていたら、この回の内訳
    （経験者が15名）が成り立たない。
    """
    people = parse_event_page(sample_html())
    assert any(p.level is Level.PICKLEBALL for p in people)
    assert any(p.level is not Level.PICKLEBALL for p in people)


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>参加者はいません</body></html>",
        "<html><script>window.__NUXT__=(function(a){return {}}(1));</script></html>",
    ],
)
def test_unreadable_pages_fail_loudly(html):
    """読み取れないときに0人で成功させない。

    tennisbear の作りが変われば壊れる。黙って「参加者がいない」と返すと、
    取り込めたつもりで空の練習会が始まってしまう。
    """
    with pytest.raises(UpstreamError):
        parse_event_page(html)


# ---------------------------------------------------------------------------
# 練習会への取り込み
# ---------------------------------------------------------------------------


def _session(db, name="取り込みの確認"):
    return sessions_service.create_session(db, name, 2)


def _participant(user_id, nickname, gender=Gender.MALE, level=Level.PICKLEBALL):
    return Participant(
        user_id=user_id, nickname=nickname, gender=gender, level=level
    )


def test_import_adds_everyone(db):
    session = _session(db)
    people = parse_event_page(sample_html())
    result = sessions_service.import_participants(db, session, people, event_id=None)
    assert len(result.added) == len(people)
    assert result.unchanged == 0
    members = sessions_service.list_members(db, session.id)
    assert {m.tennisbear_user_id for m in members} == {p.user_id for p in people}


def test_importing_twice_adds_nobody(db):
    """再取り込みしても増えない。ID で見分ける。"""
    session = _session(db)
    people = parse_event_page(sample_html())
    sessions_service.import_participants(db, session, people, event_id=None)
    again = sessions_service.import_participants(db, session, people, event_id=None)
    assert again.added == []
    assert again.unchanged == len(people)
    assert len(sessions_service.list_members(db, session.id)) == len(people)


def test_reimport_keeps_what_the_organiser_fixed(db):
    """管理者が直したレベルや性別を、取り込みで戻さない。

    レベルの推定は当てにならないので直してもらう前提。それを毎回
    上書きしたら、直す意味が無くなる。
    """
    session = _session(db)
    people = parse_event_page(sample_html())
    sessions_service.import_participants(db, session, people, event_id=None)
    target = next(p for p in people if p.level is Level.PICKLEBALL)
    member = next(
        m
        for m in sessions_service.list_members(db, session.id)
        if m.tennisbear_user_id == target.user_id
    )
    sessions_service.update_member(db, member, level=Level.BEGINNER, gender=Gender.FEMALE)

    sessions_service.import_participants(db, session, people, event_id=None)

    db.refresh(member)
    assert member.level is Level.BEGINNER, "直したレベルが戻っている"
    assert member.gender is Gender.FEMALE


def test_a_renamed_participant_is_renamed_here_too(db):
    """呼び名が変わったら追従する。古い名前で読み上げると混乱する。"""
    session = _session(db)
    sessions_service.import_participants(db, session, [_participant(2001, "旧名")], event_id=None)
    result = sessions_service.import_participants(
        db, session, [_participant(2001, "新名")],
        event_id=None,
    )
    assert result.renamed == [("旧名", "新名")]
    assert [m.nickname for m in sessions_service.list_members(db, session.id)] == ["新名"]


def test_same_names_get_a_number(db):
    """同名は2人目以降に番号を振る。先にいる人はそのまま。"""
    session = _session(db)
    result = sessions_service.import_participants(
        db,
        session,
        [_participant(3001, "マッツ"), _participant(3002, "マッツ"), _participant(3003, "マッツ")],
        event_id=None,
    )
    assert result.added == ["マッツ", "マッツ2", "マッツ3"]


def test_renaming_into_a_taken_name_also_gets_a_number(db):
    """改名先がすでに使われていたら、そこでも番号を振る。"""
    session = _session(db)
    sessions_service.import_participants(
        db, session, [_participant(4001, "マッツ"), _participant(4002, "別人")],
        event_id=None,
    )
    result = sessions_service.import_participants(
        db, session, [_participant(4001, "マッツ"), _participant(4002, "マッツ")],
        event_id=None,
    )
    assert result.renamed == [("別人", "マッツ2")]


def test_attributes_come_back_from_a_previous_session(db):
    """前の練習会で直した属性を、次の練習会でも使う。

    `member_profiles` を tennisbear の ID で引くので、改名されても見失わない。
    """
    first = _session(db, "先週")
    sessions_service.import_participants(db, first, [_participant(5001, "だれか")], event_id=None)
    member = sessions_service.list_members(db, first.id)[0]
    sessions_service.update_member(db, member, level=Level.BEGINNER)

    second = _session(db, "今週")
    sessions_service.import_participants(
        db, second, [_participant(5001, "だれか改", level=Level.PICKLEBALL)],
        event_id=None,
    )
    imported = sessions_service.list_members(db, second.id)[0]
    assert imported.level is Level.BEGINNER, "前回直したレベルが使われていない"
    assert imported.nickname == "だれか改", "今の呼び名で登録する"


# ---------------------------------------------------------------------------
# レビューで見つかった欠陥の回帰テスト
# ---------------------------------------------------------------------------


def test_a_returning_participant_does_not_break_the_import(db):
    """同名の人が過去にいても、取り込みが失敗しない。

    `member_profiles.nickname` は一意。ID で引いた行の名前を書き換えると、
    別の行とぶつかって取り込みが丸ごと 409 になり、しかも途中まで登録が残る。
    """
    first = _session(db, "先週")
    sessions_service.import_participants(
        db, first, [_participant(1, "マッツ"), _participant(2, "マッツ")],
        event_id=None,
    )
    assert [m.nickname for m in sessions_service.list_members(db, first.id)] == [
        "マッツ",
        "マッツ2",
    ]

    # 今週は2人目だけが参加する
    second = _session(db, "今週")
    result = sessions_service.import_participants(
        db, second, [_participant(2, "マッツ")],
        event_id=None,
    )
    assert result.added == ["マッツ"], "取り込みが失敗している"


def test_a_namesake_does_not_inherit_someone_elses_level(db):
    """同名の別人の属性を引き継がない。

    ID が分かっている相手に、ニックネームで当てにいってはいけない。
    """
    first = _session(db, "先週")
    sessions_service.import_participants(db, first, [_participant(1, "マッツ")], event_id=None)
    member = sessions_service.list_members(db, first.id)[0]
    sessions_service.update_member(db, member, level=Level.BEGINNER)

    second = _session(db, "今週")
    sessions_service.import_participants(
        db, second, [_participant(2, "マッツ", level=Level.PICKLEBALL)],
        event_id=None,
    )
    other = sessions_service.list_members(db, second.id)[0]
    assert other.level is Level.PICKLEBALL, "別人の直したレベルを被っている"


def test_the_same_person_twice_is_added_once(db):
    """同じ人が2回出てきても1人。

    幽霊メンバーができると、毎ラウンド出場枠を1つ食い、統計も歪む。
    """
    session = _session(db)
    result = sessions_service.import_participants(
        db, session, [_participant(7, "たろう"), _participant(7, "たろう")],
        event_id=None,
    )
    assert result.added == ["たろう"]
    assert len(sessions_service.list_members(db, session.id)) == 1


def test_a_nickname_fixed_by_hand_is_kept(db):
    """管理者が読み上げ用に付け直した名前を、取り込みで戻さない。

    上流の名前が変わったときだけ追従する。
    """
    session = _session(db)
    sessions_service.import_participants(
        db, session, [_participant(9, "とても長い表示名")],
        event_id=None,
    )
    member = sessions_service.list_members(db, session.id)[0]
    sessions_service.update_member(db, member, nickname="たろう")

    result = sessions_service.import_participants(
        db, session, [_participant(9, "とても長い表示名")],
        event_id=None,
    )
    assert result.renamed == []
    db.refresh(member)
    assert member.nickname == "たろう", "手で付けた名前が戻っている"


def test_an_upstream_rename_is_followed(db):
    """上流で改名されたら追従する。"""
    session = _session(db)
    sessions_service.import_participants(db, session, [_participant(9, "旧名")], event_id=None)
    result = sessions_service.import_participants(
        db, session, [_participant(9, "新名")],
        event_id=None,
    )
    assert result.renamed == [("旧名", "新名")]


def test_someone_who_left_the_event_is_put_to_rest(db):
    """一覧から消えた人は休憩にする。削除はしない（統計が壊れる）。"""
    session = _session(db)
    sessions_service.import_participants(
        db, session, [_participant(1, "残る人"), _participant(2, "抜ける人")],
        event_id=None,
    )
    result = sessions_service.import_participants(
        db, session, [_participant(1, "残る人")],
        event_id=None,
    )
    assert result.resting == ["抜ける人"]
    left = next(
        m for m in sessions_service.list_members(db, session.id) if m.nickname == "抜ける人"
    )
    assert left.status is MemberStatus.RESTING
    assert left.id is not None, "消してはいけない"


def test_a_failed_import_leaves_nothing_behind(db):
    """途中で失敗したら、誰も登録されない。

    1人ずつコミットしていると「先頭の数人だけ入った」状態が残る。
    """
    session = _session(db)
    people = [_participant(1, "先の人"), _participant(2, "   ")]
    with pytest.raises(ValidationError):
        sessions_service.import_participants(db, session, people, event_id=None)
    db.rollback()
    assert sessions_service.list_members(db, session.id) == []


def test_a_truncated_page_is_reported_not_crashed():
    """応答が途中で切れていても 500 にしない。"""
    broken = sample_html().split("</script>")[0]
    with pytest.raises(UpstreamError):
        parse_event_page(broken)


# ---------------------------------------------------------------------------
# API 層（ネットワークには触らない。取得だけ差し替える）
# ---------------------------------------------------------------------------


def _fake_fetch(monkeypatch, html: str | None = None, error: Exception | None = None):
    def fake(event_id, *, base_url, timeout):
        if error is not None:
            raise error
        return html

    monkeypatch.setattr("app.api.tennisbear.fetch_event_page", fake)


def test_import_endpoint_returns_a_summary(client, monkeypatch):
    from tests.test_api import create_session

    _fake_fetch(monkeypatch, sample_html())
    token = create_session(client)["token"]
    response = client.post(
        f"/api/sessions/{token}/members/import", json={"event_id": 1}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["added"]) == 18
    assert body["unchanged"] == 0
    assert body["renamed"] == []
    assert body["resting"] == []
    assert len(client.get(f"/api/sessions/{token}/members").json()) == 18


def test_importing_twice_through_the_api(client, monkeypatch):
    from tests.test_api import create_session

    _fake_fetch(monkeypatch, sample_html())
    token = create_session(client)["token"]
    client.post(f"/api/sessions/{token}/members/import", json={"event_id": 1})
    again = client.post(
        f"/api/sessions/{token}/members/import", json={"event_id": 1}
    ).json()
    assert again["added"] == []
    assert again["unchanged"] == 18


def test_an_unreadable_page_becomes_502(client, monkeypatch):
    """取り込めなかったことを、そうと分かる形で返す。"""
    from tests.test_api import create_session

    _fake_fetch(monkeypatch, "<html>参加者はいません</html>")
    token = create_session(client)["token"]
    response = client.post(
        f"/api/sessions/{token}/members/import", json={"event_id": 1}
    )
    assert response.status_code == 502
    assert response.json()["code"] == "upstream"


def test_a_network_failure_becomes_502(client, monkeypatch):
    from tests.test_api import create_session

    _fake_fetch(monkeypatch, error=UpstreamError("イベントページに接続できませんでした"))
    token = create_session(client)["token"]
    response = client.post(
        f"/api/sessions/{token}/members/import", json={"event_id": 1}
    )
    assert response.status_code == 502


def test_a_non_numeric_event_id_is_refused(client):
    from tests.test_api import create_session

    token = create_session(client)["token"]
    response = client.post(
        f"/api/sessions/{token}/members/import", json={"event_id": "abc"}
    )
    assert response.status_code == 422


def test_a_session_is_bound_to_one_event(db):
    """練習会に紐づくイベントは1つ。別のイベントは取り込ませない。

    別のイベントを入れると、その一覧に居ない人が一斉に休憩へ回る。
    イベントIDを打ち間違えたときに黙って起きると事故になる。
    """
    session = _session(db)
    sessions_service.import_participants(
        db, session, [_participant(1, "だれか")], event_id=111
    )
    assert session.tennisbear_event_id == 111

    with pytest.raises(ValidationError):
        sessions_service.import_participants(
            db, session, [_participant(2, "ほかの人")], event_id=222
        )
    db.rollback()
    assert len(sessions_service.list_members(db, session.id)) == 1


def test_the_same_event_can_be_imported_again(db):
    """同じイベントなら何度でも取り込める。直前に増えた人を足すのに使う。"""
    session = _session(db)
    sessions_service.import_participants(
        db, session, [_participant(1, "先の人")], event_id=111
    )
    result = sessions_service.import_participants(
        db, session, [_participant(1, "先の人"), _participant(2, "あとの人")], event_id=111
    )
    assert result.added == ["あとの人"]
    assert result.resting == []


def test_a_failed_import_does_not_bind_the_event(db):
    """取り込みに失敗したイベントには紐づけない。

    ここで縛ってしまうと、打ち間違えたIDが1回の失敗で居座り、
    本当に取り込みたいイベントが二度と入らなくなる。
    """
    session = _session(db)
    with pytest.raises(ValidationError):
        sessions_service.import_participants(
            db, session, [_participant(1, "   ")], event_id=111
        )
    db.rollback()
    assert session.tennisbear_event_id is None
    sessions_service.import_participants(
        db, session, [_participant(1, "だれか")], event_id=222
    )
    assert session.tennisbear_event_id == 222


def test_a_refused_import_changes_nothing(db):
    """別イベントを断っても、いま居る人には指一本触れない。

    この機能の動機がそこにある。断ったつもりで全員が休憩へ回っていたら
    元も子もないので、人数・状態・取り込み元を直接見る。
    """
    session = _session(db)
    sessions_service.import_participants(
        db, session, [_participant(1, "先の人"), _participant(2, "あとの人")], event_id=111
    )
    before = {
        member.nickname: member.status
        for member in sessions_service.list_members(db, session.id)
    }

    with pytest.raises(ValidationError):
        sessions_service.import_participants(
            db, session, [_participant(3, "よその人")], event_id=222
        )
    db.rollback()

    after = {
        member.nickname: member.status
        for member in sessions_service.list_members(db, session.id)
    }
    assert after == before
    assert all(status is MemberStatus.ACTIVE for status in after.values())
    assert session.tennisbear_event_id == 111


def test_the_api_refuses_another_event(client, monkeypatch):
    """API でも別イベントは 422 で断り、理由を日本語で返す。"""
    from tests.test_api import create_session

    _fake_fetch(monkeypatch, sample_html())
    token = create_session(client)["token"]
    first = client.post(
        f"/api/sessions/{token}/members/import", json={"event_id": 111}
    )
    assert first.status_code == 200, first.text
    count = len(client.get(f"/api/sessions/{token}/members").json())

    response = client.post(
        f"/api/sessions/{token}/members/import", json={"event_id": 222}
    )
    assert response.status_code == 422
    assert "別のイベントは取り込めません" in response.json()["detail"]
    assert len(client.get(f"/api/sessions/{token}/members").json()) == count


def test_another_event_is_refused_before_fetching(client, monkeypatch):
    """断ると分かっているイベントを取りに行かない。

    実在しないIDだと取得の失敗が先に返り、本当の理由が伝わらなくなる。
    """
    fetched: list[int] = []

    def fake(event_id, *, base_url, timeout):
        fetched.append(event_id)
        return sample_html()

    monkeypatch.setattr("app.api.tennisbear.fetch_event_page", fake)
    from tests.test_api import create_session

    token = create_session(client)["token"]
    client.post(f"/api/sessions/{token}/members/import", json={"event_id": 111})
    assert fetched == [111]

    response = client.post(
        f"/api/sessions/{token}/members/import", json={"event_id": 222}
    )
    assert response.status_code == 422
    assert fetched == [111], "断るイベントのページを取りに行っている"


def test_two_devices_cannot_bind_different_events(db, session_factory):
    """2台から同時に初めての取り込みを押しても、紐づくイベントは1つ。

    在メモリの値を見てから書くと、どちらもまだ「紐づいていない」と思って
    いるので両方が通り、あとから押した側の一覧に居ない人が休憩へ回る。
    """
    from app.models import PracticeSession

    session = _session(db)
    db.commit()

    left, right = session_factory(), session_factory()
    seen_by_left = left.get(PracticeSession, session.id)
    seen_by_right = right.get(PracticeSession, session.id)
    assert seen_by_right.tennisbear_event_id is None

    sessions_service.import_participants(
        left, seen_by_left, [_participant(1, "先の人")], event_id=111
    )

    with pytest.raises(ValidationError):
        sessions_service.import_participants(
            right, seen_by_right, [_participant(2, "よその人")], event_id=222
        )
    right.rollback()

    db.expire_all()
    assert db.get(PracticeSession, session.id).tennisbear_event_id == 111
    members = sessions_service.list_members(db, session.id)
    assert [member.nickname for member in members] == ["先の人"]
    assert all(member.status is MemberStatus.ACTIVE for member in members)
