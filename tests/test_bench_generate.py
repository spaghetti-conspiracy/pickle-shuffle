"""所要時間の計測スクリプト（scripts/bench_generate.py）。

CLAUDE.md の「性能の予算」の判断（比 1.2 超、または1秒超で諮る）を、集計が正しく印として
出すかを確かめる。計測そのものは小さな構成で1回だけ走らせる。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "bench_generate.py"
_spec = importlib.util.spec_from_file_location("bench_generate", _PATH)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def test_a_ratio_above_the_limit_is_flagged():
    """交互に測った中央値の比が 1.2 を超えたら印を付ける。"""
    mine = [{"12名3面": 130.0}, {"12名3面": 120.0}, {"12名3面": 125.0}]
    theirs = [{"12名3面": 100.0}, {"12名3面": 101.0}, {"12名3面": 99.0}]
    (row,) = bench.summarize(mine, theirs)
    assert row["mine"] == 125.0
    assert row["theirs"] == 100.0
    assert row["ratio"] == 1.25
    assert row["flag"] is True


def test_a_ratio_within_the_limit_is_not_flagged():
    mine = [{"16名4面": 230.0}, {"16名4面": 250.0}, {"16名4面": 240.0}]
    theirs = [{"16名4面": 220.0}, {"16名4面": 225.0}, {"16名4面": 210.0}]
    (row,) = bench.summarize(mine, theirs)
    assert row["ratio"] < bench.RATIO_LIMIT
    assert row["flag"] is False


def test_over_a_second_is_flagged_even_without_a_comparison():
    """どの構成でも1秒を超えたら、比べる相手がいなくても印を付ける。"""
    (row,) = bench.summarize([{"20名4面": 1100.0}], None)
    assert "ratio" not in row
    assert row["flag"] is True


def test_configs_are_parsed_with_optional_beginners():
    assert bench._parse_configs("12x3,13x3:3:2") == [(12, 3, 0, 0), (13, 3, 3, 2)]


def test_a_small_configuration_can_be_measured():
    """実際に1回測れる（小さな構成・1ラウンド）。"""
    result = bench.measure(bench.ROOT, [(8, 2, 0, 0)], rounds=1, seeds=[1])
    assert result["8名2面"] > 0
