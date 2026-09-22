"""設定値。環境変数の読み取りはこのモジュールに集約する（CLAUDE.md 不変則6）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_DATABASE_URL = "sqlite:///./data/app.db"


@dataclass(frozen=True)
class Weights:
    """生成スコアの重み。値が大きいほど、その項目を強く避ける。

    仕様書 doc/spec.md の優先度に対応する。増分形 ``2*count + 1`` は
    「二乗和の最小化」＝「回数の均等化」と等価になるように選んである。
    """

    # 優先度1: ペア・対戦の組合せがばらけること
    partner: int = 120
    opponent: int = 30
    # 優先度2: 参加回数の公平性（候補集合の slack 制限と併用する）
    fair: int = 100
    # 優先度3: 連続してマッチに入れない回数を最小にする
    sit_out: int = 60
    # 優先度3: 初心者同士のペアを避ける（実質ハード制約）
    beginner_pair: int = 100_000
    # 優先度4: 初心者と組む回数を非初心者間で均等にする
    beginner_spread: int = 40
    # 優先度5: 初心者を含むペア同士でマッチを組む
    beginner_concentration: int = 180
    # 優先度6: 男女ペア同士のマッチを優先する
    gender: int = 25
    # 優先度7: 休み明けのメンバーをなるべく早くマッチに入れる
    just_returned: int = 40
    # スキップ（不採用）した編成を再び出さないための減点。
    # beginner_pair より小さくして、「初心者同士ペアを作ってでも別編成にする」を防ぐ。
    avoid_round: int = 50_000
    avoid_match: int = 5_000


@dataclass(frozen=True)
class Settings:
    """アプリ全体の設定。"""

    database_url: str = DEFAULT_DATABASE_URL
    port: int = 8000
    weights: Weights = field(default_factory=Weights)
    # 公平性の「枠」をどこまで緩めてよいか（試合数の差の上限）。
    fairness_slack_max: int = 1
    # 候補集合がこの数を下回る間だけ slack を広げる（PLAN.md 設計の要 B）。
    min_candidate_sets: int = 8
    # 1回の生成で評価する候補集合の上限。超える分はサンプリングする。
    max_candidate_sets: int = 60


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_settings() -> Settings:
    """環境変数から設定を読み込む。"""
    return Settings(
        database_url=os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL,
        port=_env_int("PORT", 8000),
        fairness_slack_max=_env_int("FAIRNESS_SLACK_MAX", 1),
        min_candidate_sets=_env_int("MIN_CANDIDATE_SETS", 8),
        max_candidate_sets=_env_int("MAX_CANDIDATE_SETS", 60),
    )


settings = load_settings()
