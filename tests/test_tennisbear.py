"""tennisbear のイベントページの解析と、練習会への取り込み。

ネットワークには触らない。解析は保存したページで確かめる。

フィクスチャは**実際のイベントページから作った**ものに、氏名・ユーザID・画像URLだけ
差し替えを入れてある。埋め込まれた状態の形（短縮変数を含む）と、レベル・性別の
分布は実物のまま。実際にどういう顔ぶれだったか分かっているので、期待値に使える。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from app.errors import UpstreamError
from app.scheduler.domain import Gender, Level
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
    result = sessions_service.import_participants(db, session, people)
    assert len(result.added) == len(people)
    assert result.unchanged == 0
    members = sessions_service.list_members(db, session.id)
    assert {m.tennisbear_user_id for m in members} == {p.user_id for p in people}


def test_importing_twice_adds_nobody(db):
    """再取り込みしても増えない。ID で見分ける。"""
    session = _session(db)
    people = parse_event_page(sample_html())
    sessions_service.import_participants(db, session, people)
    again = sessions_service.import_participants(db, session, people)
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
    sessions_service.import_participants(db, session, people)
    target = next(p for p in people if p.level is Level.PICKLEBALL)
    member = next(
        m
        for m in sessions_service.list_members(db, session.id)
        if m.tennisbear_user_id == target.user_id
    )
    sessions_service.update_member(db, member, level=Level.BEGINNER, gender=Gender.FEMALE)

    sessions_service.import_participants(db, session, people)

    db.refresh(member)
    assert member.level is Level.BEGINNER, "直したレベルが戻っている"
    assert member.gender is Gender.FEMALE


def test_a_renamed_participant_is_renamed_here_too(db):
    """呼び名が変わったら追従する。古い名前で読み上げると混乱する。"""
    session = _session(db)
    sessions_service.import_participants(db, session, [_participant(2001, "旧名")])
    result = sessions_service.import_participants(
        db, session, [_participant(2001, "新名")]
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
    )
    assert result.added == ["マッツ", "マッツ2", "マッツ3"]


def test_renaming_into_a_taken_name_also_gets_a_number(db):
    """改名先がすでに使われていたら、そこでも番号を振る。"""
    session = _session(db)
    sessions_service.import_participants(
        db, session, [_participant(4001, "マッツ"), _participant(4002, "別人")]
    )
    result = sessions_service.import_participants(
        db, session, [_participant(4001, "マッツ"), _participant(4002, "マッツ")]
    )
    assert result.renamed == [("別人", "マッツ2")]


def test_attributes_come_back_from_a_previous_session(db):
    """前の練習会で直した属性を、次の練習会でも使う。

    `member_profiles` を tennisbear の ID で引くので、改名されても見失わない。
    """
    first = _session(db, "先週")
    sessions_service.import_participants(db, first, [_participant(5001, "だれか")])
    member = sessions_service.list_members(db, first.id)[0]
    sessions_service.update_member(db, member, level=Level.BEGINNER)

    second = _session(db, "今週")
    sessions_service.import_participants(
        db, second, [_participant(5001, "だれか改", level=Level.PICKLEBALL)]
    )
    imported = sessions_service.list_members(db, second.id)[0]
    assert imported.level is Level.BEGINNER, "前回直したレベルが使われていない"
    assert imported.nickname == "だれか改", "今の呼び名で登録する"
