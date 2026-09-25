"""1生成（ボタンを押してから次のカードが出るまで）の所要時間を測る。手で実行する。

CLAUDE.md の「性能の予算」の計測に使う。

    uv run python scripts/bench_generate.py                   # このチェックアウトを測る
    uv run python scripts/bench_generate.py --against ../main  # 変更前と交互に測って比べる

``--against`` には、比べる相手のチェックアウト（例: main を `git worktree add` したもの）を渡す。
同じマシンで、このチェックアウトと相手を**交互に** ``--repeat`` 回ずつ測り、構成ごとの中央値と
その比（このチェックアウト / 相手）を出す。マシンの負荷で1〜2割ぶれるので、1回ずつの比較では
判断しない。回ごとに先に測る側を入れ替えて、順番による偏りを減らす。

印を付ける条件（CLAUDE.md）: 比が 1.2 を超えた構成、平均が1秒を超えた構成。1秒の判定は平均で
行う（ユーザー判断。doc/algorithm.md の表も平均）。1生成ごとの最大は参考として表に出す。

測り方: 各構成を ``--seeds`` の各シードで ``--rounds`` ラウンド回し、1生成あたりの平均と最大（ms）。
既定では 20名4面 だけで1本あたり約70秒（1生成が約1秒 x 24ラウンド x 3シード）かかり、
``--against`` を付けると両側を ``--repeat`` 回ずつ走らせる。
生成は `tests/simulation.py` の Simulator（本番と同じ導出規則）を通す。
相手のチェックアウトにこのスクリプトが無くても測れるよう、各回は別プロセスで、測るコードの
場所（``--root``）を先頭に置いて読み込む。読み込んだコードが ``--root`` の下になければ止める
（相手に ``app/`` が無いと、インストール済みの自分側のコードを黙って測ってしまうため）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (人数, 面数, 初心者, ラケット経験者)。CLAUDE.md の「評価する構成」（13名・12名・16名を
# 2面と3面で）と、表で最も遅いもの（16名3面・20名4面）、初心者入りの構成。
DEFAULT_CONFIGS = [
    (12, 2, 0, 0), (12, 3, 0, 0), (13, 2, 0, 0), (13, 3, 0, 0), (13, 3, 3, 2),
    (16, 2, 0, 0), (16, 3, 0, 0), (16, 4, 0, 0), (20, 4, 0, 0),
]
RATIO_LIMIT = 1.2
"""この比を超えて遅くなったら諮る（CLAUDE.md）。"""
SECOND_MS = 1000.0
"""どの構成でも、平均がこれを超えたら諮る（CLAUDE.md）。"""


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
) -> dict[str, dict[str, float]]:
    """``root`` のコードで構成ごとに測り、1生成あたりの平均と最大（ms）を返す。"""
    sys.path.insert(0, str(root))
    import app.scheduler.generator
    import tests.simulation
    from tests.simulation import Simulator, make_members

    for module in (app.scheduler.generator, tests.simulation):
        if not Path(module.__file__).resolve().is_relative_to(root):
            raise SystemExit(
                f"{root} ではなく {module.__file__} を読み込んだ。パスを確かめてください"
            )

    result: dict[str, dict[str, float]] = {}
    for config in configs:
        count, courts, beginners, racket = config
        times: list[float] = []
        for seed in seeds:
            members = make_members(count, beginners=beginners, racket=racket, beginners_last=True)
            sim = Simulator(members, seed=seed, court_count=courts)
            for _ in range(rounds):
                start = time.perf_counter()
                plan = sim.generate()
                times.append((time.perf_counter() - start) * 1000)
                sim.adopt(plan)
        result[label(config)] = {"mean": statistics.mean(times), "max": max(times)}
    return result


def summarize(
    mine: list[dict[str, dict[str, float]]], theirs: list[dict[str, dict[str, float]]] | None
) -> list[dict[str, object]]:
    """構成ごとの平均の中央値と比、最大。印の理由を ``reasons`` に並べる。"""
    rows: list[dict[str, object]] = []
    for name in mine[0]:
        median = statistics.median(run[name]["mean"] for run in mine)
        reasons = ["平均1秒超"] if median > SECOND_MS else []
        row: dict[str, object] = {
            "config": name,
            "mine": median,
            "max": max(run[name]["max"] for run in mine),
            "reasons": reasons,
        }
        if theirs:
            base = statistics.median(run[name]["mean"] for run in theirs)
            ratio = median / base
            row.update(theirs=base, ratio=ratio)
            if ratio > RATIO_LIMIT:
                reasons.append("比 1.2 超")
        rows.append(row)
    return rows


def _check_root(root: Path) -> None:
    """測るチェックアウトに、生成器とシミュレータがあるかを先に確かめる。"""
    for part in ("app/scheduler/generator.py", "tests/simulation.py"):
        if not (root / part).is_file():
            raise SystemExit(f"{root} に {part} が無い。チェックアウトのパスを確かめてください")


def _run_once(root: Path, args: argparse.Namespace) -> dict[str, dict[str, float]]:
    """別プロセスで ``root`` のコードを測る（読み込んだモジュールが混ざらないように）。"""
    command = [
        sys.executable, __file__, "--measure-only", "--root", str(root),
        "--rounds", str(args.rounds), "--seeds", args.seeds_text, "--configs", args.configs_text,
    ]
    # stderr は捕まえずに流す（失敗したときに原因がそのまま見える）。
    done = subprocess.run(command, stdout=subprocess.PIPE, text=True)
    if done.returncode != 0:
        raise SystemExit(f"{root} の計測に失敗した（上のエラーを参照）")
    return json.loads(done.stdout.strip().splitlines()[-1])


def _parse_configs(text: str) -> list[tuple[int, int, int, int]]:
    """"12x3,13x3:3:2" → [(12, 3, 0, 0), (13, 3, 3, 2)]。"""
    configs = []
    for item in text.split(","):
        size, _, rest = item.partition(":")
        count, courts = (int(v) for v in size.split("x"))
        beginners, racket = (int(v) for v in rest.split(":")) if rest else (0, 0)
        configs.append((count, courts, beginners, racket))
    return configs


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("1以上を指定してください")
    return value


def _pad(text: str, width: int, *, right: bool = False) -> str:
    """全角を2桁として数え、表示幅 ``width`` まで空白で埋める。``right`` なら右寄せ。"""
    shown = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    space = " " * max(0, width - shown)
    return space + text if right else text + space


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--against", type=Path, help="比べる相手のチェックアウト")
    parser.add_argument("--repeat", type=_positive, default=3, help="交互に測る回数（既定 3）")
    parser.add_argument("--rounds", type=_positive, default=24)
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
        root = args.root.resolve()
        print(json.dumps(measure(root, configs, rounds=args.rounds, seeds=seeds)))
        return

    if not args.against and args.repeat != 3:
        print("--against が無いので --repeat は使わない（1回だけ測る）", file=sys.stderr)
    sides = [("このチェックアウト", ROOT)]
    if args.against:
        sides.append(("相手", args.against.resolve()))
    for _name, root in sides:
        _check_root(root)
        print(f"{_name}: {root}")

    runs: dict[str, list[dict[str, dict[str, float]]]] = {name: [] for name, _ in sides}
    for turn in range(args.repeat if args.against else 1):
        # 回ごとに先に測る側を入れ替える（ABBA）。温度などによる順番の偏りを減らす。
        order = sides if turn % 2 == 0 else list(reversed(sides))
        for name, root in order:
            print(f"  {name} を測っている（{turn + 1}回目）…", file=sys.stderr, flush=True)
            runs[name].append(_run_once(root, args))

    print(f"\n1生成あたり（ms）。{args.rounds}ラウンド x シード {args.seeds_text}", end="")
    print(f"、交互に {args.repeat} 回ずつの中央値" if args.against else "")
    header = f"  {_pad('構成', 30)} {_pad('平均', 8, right=True)} {_pad('最大', 8, right=True)}"
    if args.against:
        header += f" {_pad('相手の平均', 10, right=True)} {_pad('比', 6, right=True)}"
    print(header)
    for row in summarize(runs["このチェックアウト"], runs.get("相手")):
        line = f"  {_pad(row['config'], 30)} {row['mine']:>8.0f} {row['max']:>8.0f}"
        if "ratio" in row:
            line += f" {row['theirs']:>10.0f} {row['ratio']:>6.2f}"
        if row["reasons"]:
            line += "   ← 要相談: " + "・".join(row["reasons"])
        print(line)


if __name__ == "__main__":
    main()
