"""所要時間の計測スクリプト（scripts/bench_generate.py）。

CLAUDE.md の「性能の予算」の判断（比 1.2 超、または1秒超で諮る）を、集計が正しく印として
出すかを確かめる。計測そのものは小さな構成で1回だけ走らせる。
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "bench_generate.py"
_spec = importlib.util.spec_from_file_location("bench_generate", _PATH)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def _runs(name: str, means: list[float]) -> list[dict[str, dict[str, float]]]:
    return [{name: {"mean": m, "max": m * 2}} for m in means]


def test_a_ratio_above_the_limit_is_flagged():
    """交互に測った中央値の比が 1.2 を超えたら印を付ける。比の向きは 自分 / 相手。"""
    mine = _runs("12名3面", [130.0, 120.0, 125.0])
    theirs = _runs("12名3面", [100.0, 101.0, 99.0])
    (row,) = bench.summarize(mine, theirs)
    assert row["mine"] == 125.0
    assert row["theirs"] == 100.0
    assert row["ratio"] == 1.25
    assert row["reasons"] == ["比 1.2 超"]


def test_a_ratio_within_the_limit_is_not_flagged():
    mine = _runs("16名4面", [230.0, 250.0, 240.0])
    theirs = _runs("16名4面", [220.0, 225.0, 210.0])
    (row,) = bench.summarize(mine, theirs)
    assert row["ratio"] < bench.RATIO_LIMIT
    assert row["reasons"] == []


def test_over_a_second_on_average_is_flagged_even_without_a_comparison():
    """平均が1秒を超えたら、比べる相手がいなくても印を付ける（判定は平均。ユーザー判断）。"""
    (row,) = bench.summarize(_runs("20名4面", [1100.0]), None)
    assert "ratio" not in row
    assert row["reasons"] == ["平均1秒超"]


def test_the_maximum_is_shown_but_does_not_flag():
    """1生成ごとの最大は全部の回を通した最大として出すが、印は平均で判定する。"""
    (row,) = bench.summarize(_runs("20名4面", [900.0, 800.0, 700.0]), None)
    assert row["max"] == 1800.0
    assert row["reasons"] == []


def test_configs_are_parsed_with_optional_beginners():
    assert bench._parse_configs("12x3,13x3:3:2") == [(12, 3, 0, 0), (13, 3, 3, 2)]


def test_a_small_configuration_can_be_measured():
    """実際に1回測れる（小さな構成・1ラウンド）。"""
    result = bench.measure(bench.ROOT, [(8, 2, 0, 0)], rounds=1, seeds=[1])
    assert result["8名2面"]["mean"] > 0


_DUMMY_SIMULATION = """
import time


def make_members(count, beginners=0, racket=0, beginners_last=False):
    return list(range(count))


class Simulator:
    def __init__(self, members, seed, court_count):
        pass

    def generate(self):
        time.sleep(0.25)
        return None

    def adopt(self, plan):
        pass
"""


def _dummy_checkout(root: Path, *, with_app: bool = True) -> Path:
    """1生成に 250ms かかるだけの、最小のチェックアウト。

    本物の 8名2面 は数十 ms なので、どちらを測ったかを見分けられる。
    """
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "simulation.py").write_text(_DUMMY_SIMULATION)
    if with_app:
        (root / "app" / "scheduler").mkdir(parents=True)
        (root / "app" / "__init__.py").write_text("")
        (root / "app" / "scheduler" / "__init__.py").write_text("")
        (root / "app" / "scheduler" / "generator.py").write_text("")
    return root


def _args() -> argparse.Namespace:
    return argparse.Namespace(rounds=2, seeds_text="1", configs_text="8x2")


def test_the_other_checkout_is_what_gets_measured(tmp_path):
    """--against の相手は、別プロセスで相手側のコードを読み込んで測る。"""
    result = bench._run_once(_dummy_checkout(tmp_path / "other"), _args())
    assert result["8名2面"]["mean"] >= 250


def test_a_checkout_without_the_generator_is_refused(tmp_path):
    """生成器やシミュレータが無いパスは、最初に分かるメッセージで止める。"""
    with pytest.raises(SystemExit, match="app/scheduler/generator.py"):
        bench._check_root(tmp_path)


def test_installed_code_is_not_measured_in_place_of_the_other(tmp_path):
    """相手に app/ が無いと、インストール済みの自分側の app を黙って測りかねない。止める。"""
    with pytest.raises(SystemExit, match="計測に失敗"):
        bench._run_once(_dummy_checkout(tmp_path / "noapp", with_app=False), _args())
