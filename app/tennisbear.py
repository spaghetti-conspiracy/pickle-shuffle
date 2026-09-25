"""tennisbear.net のイベントページから参加者を読み取る。

練習会のたびに十数人を手で打ち込むのが重いので、イベントID を入れたら
ニックネーム・性別・レベルを取り込めるようにする。

**公開 API が無いので、ページに埋め込まれた状態を読む。** サイトの作りが
変われば壊れる。壊れたときに黙って0人にならないよう、読み取れなければ
例外にする（:class:`app.errors.UpstreamError`）。

解析（:func:`parse_event_page`）はネットワークに触らない純粋関数にしてある。
テストはここに集中させ、取得側は差し替える。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.errors import UpstreamError
from app.scheduler.domain import Gender, Level

#: 取得する応答の上限。実ページは 300KB 程度なので十分な余裕がある。
#: 上限が無いと、相手の作りが変わったときにメモリを食い尽くしかねない。
MAX_PAGE_BYTES = 8 * 1024 * 1024

#: 短縮された変数の名前。JavaScript なので `$` を含みうる。
_JS_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

#: 参加者一覧が入っている配列の名前。
_PARTICIPANTS_KEY = "participantList:["

#: ページに埋め込まれた状態の始まり。Nuxt が即時実行関数の形で書き出す。
_STATE_HEAD = "__NUXT__=(function("

#: tennisbear の性別表記。これ以外（未記入など）は「未設定」にする。
_GENDERS = {"男性": Gender.MALE, "女性": Gender.FEMALE}

#: レベルが未設定・非公開のときに、どの段階とみなすか。
#: 伏せている人は経験者のことも多いが、初心者を取りこぼして
#: 「成立しない試合」ができる方が害が大きいので、低い方に寄せる。
UNKNOWN_LEVEL = 1

#: ピックルボールのレベルがこれなら「まだ始めていない」。
_PICKLEBALL_NOVICE = 1

#: テニスのレベルがこれ以下なら、ラケットスポーツ自体が未経験とみなす。
#: 1=はじめて, 2=初心者。
_RACKET_NOVICE_MAX = 2


@dataclass(frozen=True)
class Participant:
    """取り込む1人分。ここに無いもの（画像や連絡先）は読み取らない。"""

    user_id: int
    nickname: str
    gender: Gender
    level: Level


def level_from_tennisbear(tennis_level: int, pickleball_level: int) -> Level:
    """tennisbear の2つのレベルを、このアプリのレベルに対応させる。

    tennisbear は テニスとピックルボールのレベルを別々に持っている。
    どちらも 1=はじめて 2=初心者 3=初級 4=初中級 5=中級 6=中上級 …。

    ピックルボールを始めていない人のうち、ラケットスポーツも未経験なら
    「初心者」、テニスの経験があるなら「ラケット経験者」になる。
    この違いは仕様3a/3b の避け方を変えるので、混ぜてはいけない。

    推定なので外れる。管理画面で直せることが前提。

    外れ方の実例: テニスを「初中級」で登録しているが、実際にはラケット
    スポーツ未経験、という人がいた。登録値が本人の実態と違うので、
    ここから見分ける方法は無い。取り込みは下書きにすぎない。
    """
    if pickleball_level > _PICKLEBALL_NOVICE:
        return Level.PICKLEBALL
    if tennis_level <= _RACKET_NOVICE_MAX:
        return Level.BEGINNER
    return Level.RACKET_EXPERIENCED


def _split_top_level(source: str, separator: str = ",") -> list[str]:
    """括弧と文字列の中を無視して区切る。"""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    escaped = False
    buffer: list[str] = []
    for char in source:
        if quote is not None:
            buffer.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            buffer.append(char)
            continue
        if char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    parts.append("".join(buffer))
    return parts


def _variable_table(state: str) -> dict[str, str]:
    """短縮された変数を実際の値に戻す表を作る。

    埋め込まれた状態は ``(function(a,b,c){return {...}}("男性","女性",...))``
    の形で、値の多くが仮引数に置き換えられている。仮引数の並びと
    末尾の実引数の並びを突き合わせれば元に戻せる。
    """
    # `__NUXT__=(function(a,b,…)` なので、`function` の直後の括弧から切る。
    # 最初の括弧から切ると params[0] が "function(a" になり、先頭の変数
    # （実ページでは null）が表に載らない。
    opening = state.index("(", len(_STATE_HEAD) - 1)
    params = state[opening + 1 : state.index(")", opening)].split(",")
    # JavaScript の変数名には `$` が使える（`a$` など）。Python の
    # isidentifier() は弾いてしまうので、自前で確かめる。
    if not all(_JS_NAME.fullmatch(p.strip()) for p in params):
        raise UpstreamError("イベントページの形式が変わっているようです")
    body = state.rstrip().rstrip(";").rstrip(")")
    tail = body.rfind("}(")
    if tail < 0:
        raise UpstreamError("イベントページの形式が変わっているようです")
    args = _split_top_level(body[tail + 2 :])
    if len(params) != len(args):
        raise UpstreamError("イベントページの形式が変わっているようです")
    return dict(zip(params, args, strict=True))


def _literal(token: str, variables: dict[str, str]) -> str | None:
    """短縮変数を解いて、JavaScript の値を Python の値にする。"""
    token = variables.get(token, token).strip()
    if token in ("void 0", "undefined", "null"):
        return None
    if token.startswith('"'):
        try:
            return json.loads(token)
        except json.JSONDecodeError:
            return token.strip('"')
    return token


def _entries(participants: str) -> list[str]:
    """参加者の配列を1人ずつに切り出す。

    正規表現を配列全体にかけると、項目が欠けている人がいたときに
    隣の人の値を拾ってしまう。括弧の対応で区切ってから読む。
    """
    depth = 0
    start = None
    quote: str | None = None
    escaped = False
    out: list[str] = []
    for index, char in enumerate(participants):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(participants[start : index + 1])
                start = None
        elif char == "]" and depth == 0:
            break
    return out


def _level_id(entry: str, key: str, variables: dict[str, str]) -> int:
    """``level`` / ``pickleballLevel`` の段階を読む。未設定なら既定値。"""
    found = re.search(rf"(?<![A-Za-z]){key}:\{{id:([A-Za-z_$0-9]+)", entry)
    if found is None:
        return UNKNOWN_LEVEL
    value = _literal(found.group(1), variables)
    if value is None or not str(value).isdigit():
        return UNKNOWN_LEVEL
    return int(value)


def parse_event_page(html: str) -> list[Participant]:
    """イベントページの HTML から参加者を取り出す。

    読み取れなければ :class:`UpstreamError`。0人で成功させると、
    サイトの作りが変わったときに「参加者がいない」と誤解させてしまう。
    """
    head = html.find(_STATE_HEAD)
    if head < 0:
        raise UpstreamError("イベントページを読み取れませんでした")
    end = html.find("</script>", head)
    if end < 0:
        # 応答が途中で切れた。500 にせず、取り込めなかったと伝える。
        raise UpstreamError("イベントページを最後まで読み取れませんでした")
    state = html[head:end]
    variables = _variable_table(state)

    key = state.find(_PARTICIPANTS_KEY)
    if key < 0:
        # 存在しないイベントでも 200 が返ってくる（中身が無いだけ）ので、
        # ここに落ちる原因はたいてい ID の間違い。
        raise UpstreamError("参加者の一覧が見つかりませんでした。イベントIDをご確認ください")
    entries = _entries(state[key + len(_PARTICIPANTS_KEY) :])

    participants: list[Participant] = []
    for entry in entries:
        found = re.search(
            r"user:\{id:([A-Za-z_$0-9]+),name:((?:\"(?:[^\"\\]|\\.)*\")|[A-Za-z_$]+)",
            entry,
        )
        if found is None:
            continue
        user_id = _literal(found.group(1), variables)
        nickname = _literal(found.group(2), variables)
        if user_id is None or nickname is None or not str(user_id).isdigit():
            continue
        gender_found = re.search(r"gender:\{name:([A-Za-z_$]+|\"[^\"]*\")", entry)
        gender_name = _literal(gender_found.group(1), variables) if gender_found else None
        participants.append(
            Participant(
                user_id=int(user_id),
                nickname=str(nickname).strip(),
                gender=_GENDERS.get(str(gender_name), Gender.OTHER),
                level=level_from_tennisbear(
                    _level_id(entry, "level", variables),
                    _level_id(entry, "pickleballLevel", variables),
                ),
            )
        )

    if not participants:
        raise UpstreamError("参加者を読み取れませんでした。イベントIDをご確認ください")
    return participants


def fetch_event_page(event_id: int, *, base_url: str, timeout: float) -> str:
    """イベントページを取ってくる。ここだけがネットワークに触る。"""
    url = f"{base_url.rstrip('/')}/pickleball/event/{event_id}/info"
    request = urllib.request.Request(url, headers={"User-Agent": "pickle-shuffle/0.1 (practice session tool)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_PAGE_BYTES + 1)
        if len(raw) > MAX_PAGE_BYTES:
            raise UpstreamError("イベントページが大きすぎます")
        return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpstreamError(f"イベント {event_id} が見つかりませんでした") from error
        raise UpstreamError(f"イベントページを取得できませんでした（{error.code}）") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpstreamError("イベントページに接続できませんでした") from error
