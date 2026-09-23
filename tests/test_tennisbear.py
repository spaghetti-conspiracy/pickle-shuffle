"""tennisbear のイベントページの解析と、練習会への取り込み。

ネットワークには触らない。取得は差し替え、解析は保存した架空のページで確かめる。
"""

from __future__ import annotations

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

FIXTURE = Path(__file__).parent / "fixtures" / "tennisbear_event.html"


def sample_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


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
    """参加者を取りこぼさない。"""
    people = parse_event_page(sample_html())
    assert [p.nickname for p in people] == [
        "はじめ",
        "ラケットさん",
        "未設定さん",
        "経験者",
        "Emoji/スラッシュ🏓",
    ]
    assert [p.user_id for p in people] == [1001, 1002, 1003, 1004, 1005]


def test_reads_gender_and_level():
    people = {p.nickname: p for p in parse_event_page(sample_html())}
    assert people["はじめ"].gender is Gender.MALE
    assert people["ラケットさん"].gender is Gender.FEMALE
    assert people["はじめ"].level is Level.BEGINNER
    assert people["ラケットさん"].level is Level.RACKET_EXPERIENCED
    assert people["経験者"].level is Level.PICKLEBALL


def test_missing_values_fall_back_to_the_cautious_side():
    """レベルも性別も未設定の人。

    レベルは低い方に寄せる。初心者を取りこぼして「成立しない試合」が
    できる方が、多めに拾って管理者が直すより害が大きい。
    """
    people = {p.nickname: p for p in parse_event_page(sample_html())}
    assert people["未設定さん"].level is Level.BEGINNER
    assert people["未設定さん"].gender is Gender.OTHER


def test_escapes_and_emoji_survive():
    """名前のエスケープを戻す。読み上げる名前なので化けさせない。"""
    people = [p.nickname for p in parse_event_page(sample_html())]
    assert "Emoji/スラッシュ🏓" in people


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
    result = sessions_service.import_participants(
        db, session, parse_event_page(sample_html())
    )
    assert len(result.added) == 5
    assert result.unchanged == 0
    members = sessions_service.list_members(db, session.id)
    assert {m.tennisbear_user_id for m in members} == {1001, 1002, 1003, 1004, 1005}


def test_importing_twice_adds_nobody(db):
    """再取り込みしても増えない。ID で見分ける。"""
    session = _session(db)
    people = parse_event_page(sample_html())
    sessions_service.import_participants(db, session, people)
    again = sessions_service.import_participants(db, session, people)
    assert again.added == []
    assert again.unchanged == 5
    assert len(sessions_service.list_members(db, session.id)) == 5


def test_reimport_keeps_what_the_organiser_fixed(db):
    """管理者が直したレベルや性別を、取り込みで戻さない。

    レベルの推定は当てにならないので直してもらう前提。それを毎回
    上書きしたら、直す意味が無くなる。
    """
    session = _session(db)
    people = parse_event_page(sample_html())
    sessions_service.import_participants(db, session, people)
    member = next(
        m
        for m in sessions_service.list_members(db, session.id)
        if m.tennisbear_user_id == 1004
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
