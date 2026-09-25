"""1生成（ボタンを押してから次のカードが出るまで）の所要時間を測る。手で実行する。

CLAUDE.md の「性能の予算」の計測に使う。

    uv run python scripts/bench_generate.py                   # このチェックアウトを測る
    uv run python scripts/bench_generate.py --against ../main  # 変更前と交互に測って比べる

``--against`` には、比べる相手のチェックアウト（例: main を `git worktree add` したもの）を渡す。
同じマシンで、このチェックアウトと相手を**交互に** ``--repeat`` 回ずつ測り、構成ごとの中央値と
その比（このチェックアウト / 相手）を出す。マシンの負荷で1〜2割ぶれるので、1回ずつの比較では
判断しない。比が 1.2 を超えた構成と、1秒を超えた構成には印を付ける。

測り方: 各構成を ``--seeds`` の各シードで ``--rounds`` ラウンド回し、1生成あたりの平均（ms）。
生成は `tests/simulation.py` の Simulator（本番と同じ導出規則）を通す。
相手のチェックアウトにこのスクリプトが無くても測れるよう、各回は別プロセスで、測るコードの
場所（``--root``）を先頭に置いて読み込む。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (人数, 面数, 初心者, ラケット経験者)。性能の予算で見る構成と、表で最も遅いもの。
DEFAULT_CONFIGS = [(12, 3, 0, 0), (13, 3, 3, 2), (16, 4, 0, 0), (16, 3, 0, 0), (20, 4, 0, 0)]
RATIO_LIMIT = 1.2
"""この比を超えて遅くなったら諮る（CLAUDE.md）。"""
SECOND_MS = 1000.0
"""どの構成でも、これを超えたら諮る（CLAUDE.md）。"""


def label(config: tuple[int, int, int, int]) -> str:
    count, courts, beginners, racket = config
    extra = f"（初心者{beginners}・ラケット{racket}）" if beginners or racket else ""
    return f"{count}名{courts}面{extra}"


def measure(
    root: Path,
    configs: list[tuple[int, int, int, int]],
    *,
    rounds: int,
    seeds: list[int],
) -> dict[str, float]:
    """``root`` のコードで構成ごとに測り、1生成あたりの平均（ms）を返す。"""
    sys.path.insert(0, str(root))
    from tests.simulation import Simulator, make_members

    result: dict[str, float] = {}
    for config in configs:
        count, courts, beginners, racket = config
        total = 0.0
        runs = 0
        for seed in seeds:
            members = make_members(count, beginners=beginners, racket=racket, beginners_last=True)
            sim = Simulator(members, seed=seed, court_count=courts)
            for _ in range(rounds):
                start = time.perf_counter()
                plan = sim.generate()
                total += time.perf_counter() - start
                runs += 1
                sim.adopt(plan)
        result[label(config)] = total / runs * 1000
    return result


def summarize(
    mine: list[dict[str, float]], theirs: list[dict[str, float]] | None
) -> list[dict[str, object]]:
    """構成ごとの中央値と比。比が 1.2 超、または1秒超なら ``flag`` を立てる。"""
    rows: list[dict[str, object]] = []
    for name in mine[0]:
        median = statistics.median(run[name] for run in mine)
        row: dict[str, object] = {"config": name, "mine": median, "flag": median > SECOND_MS}
        if theirs:
            base = statistics.median(run[name] for run in theirs)
            ratio = median / base
            row.update(theirs=base, ratio=ratio, flag=row["flag"] or ratio > RATIO_LIMIT)
        rows.append(row)
    return rows


def _run_once(root: Path, args: argparse.Namespace) -> dict[str, float]:
    """別プロセスで ``root`` のコードを測る（読み込んだモジュールが混ざらないように）。"""
    command = [
        sys.executable, __file__, "--measure-only", "--root", str(root),
        "--rounds", str(args.rounds), "--seeds", args.seeds_text, "--configs", args.configs_text,
    ]
    output = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    return json.loads(output.strip().splitlines()[-1])


def _parse_configs(text: str) -> list[tuple[int, int, int, int]]:
    """"12x3,13x3:3:2" → [(12, 3, 0, 0), (13, 3, 3, 2)]。"""
    configs = []
    for item in text.split(","):
        size, _, rest = item.partition(":")
        count, courts = (int(v) for v in size.split("x"))
        beginners, racket = (int(v) for v in rest.split(":")) if rest else (0, 0)
        configs.append((count, courts, beginners, racket))
    return configs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--against", type=Path, help="比べる相手のチェックアウト")
    parser.add_argument("--repeat", type=int, default=3, help="交互に測る回数（既定 3）")
    parser.add_argument("--rounds", type=int, default=24)
    parser.add_argument("--seeds", dest="seeds_text", default="11,22,33")
    parser.add_argument(
        "--configs",
        dest="configs_text",
        default=",".join(f"{n}x{c}:{b}:{r}" for n, c, b, r in DEFAULT_CONFIGS),
        help="人数x面数[:初心者:ラケット経験者] をカンマ区切り",
    )
    parser.add_argument("--measure-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    seeds = [int(s) for s in args.seeds_text.split(",")]
    configs = _parse_configs(args.configs_text)

    if args.measure_only:
        print(json.dumps(measure(args.root.resolve(), configs, rounds=args.rounds, seeds=seeds)))
        return

    mine: list[dict[str, float]] = []
    theirs: list[dict[str, float]] = []
    for _ in range(args.repeat if args.against else 1):
        mine.append(_run_once(ROOT, args))
        if args.against:
            theirs.append(_run_once(args.against.resolve(), args))

    print(f"1生成あたりの平均（ms）。{args.rounds}ラウンド x シード {args.seeds_text}", end="")
    print(f"、交互に {args.repeat} 回ずつの中央値" if args.against else "")
    for row in summarize(mine, theirs or None):
        line = f"  {row['config']:<24} {row['mine']:>8.0f}"
        if "ratio" in row:
            line += f"   相手 {row['theirs']:>8.0f}   比 {row['ratio']:.2f}"
        if row["flag"]:
            line += "   ← 要相談（比 1.2 超、または1秒超）"
        print(line)


if __name__ == "__main__":
    main()
