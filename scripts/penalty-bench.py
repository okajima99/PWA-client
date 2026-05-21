#!/usr/bin/env python3
"""Penalty regression bench.

`rate_limits_log.jsonl` を読んで「直近 N turn の 5h_pct delta」 を集計する。
PTY 移行の各 phase 完了時に走らせて、 baseline (= +1-2%) からドリフトしてないかを確認する。

参考レンジ (= 5/21 計測):
    terminal 直叩き / PTY 経由 (clsh) : +1-2% / turn
    旧 PWA (Agent SDK + proxy)         : +13% / turn

使い方:
    python3 scripts/penalty-bench.py                          # default log path
    python3 scripts/penalty-bench.py /path/to/rate-limits.jsonl
    BENCH_N=20 python3 scripts/penalty-bench.py               # 直近 20 turn で集計
"""
import json
import os
import sys
from pathlib import Path

DEFAULT_LOG_ENV = "RATE_LIMITS_LOG_PATH"


def main() -> int:
    if len(sys.argv) > 1:
        log_path = Path(sys.argv[1])
    elif os.environ.get(DEFAULT_LOG_ENV):
        log_path = Path(os.environ[DEFAULT_LOG_ENV])
    else:
        print(
            f"ERR: pass log path as argv[1] or set ${DEFAULT_LOG_ENV} "
            "(`backend/config.json` の `rate_limits_log` キーと同じ値)",
            file=sys.stderr,
        )
        return 2

    if not log_path.exists():
        print(f"ERR: log not found at {log_path}", file=sys.stderr)
        return 2

    entries: list[dict] = []
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    n = int(os.environ.get("BENCH_N", "10"))
    # 直近 N 倍引いて、 five_hour_pct を持つ entry を後ろから N 件
    candidates = [e for e in entries if e.get("five_hour_pct") is not None]
    recent = candidates[-n:]
    if len(recent) < 2:
        print(
            f"ERR: only {len(recent)} entry with five_hour_pct, need >= 2",
            file=sys.stderr,
        )
        return 2

    # 隣接 turn の差分。 5h window reset を跨ぐと負値になる、 その pair は skip
    deltas: list[float] = []
    for prev, cur in zip(recent, recent[1:]):
        d = cur["five_hour_pct"] - prev["five_hour_pct"]
        if d < 0:
            continue
        deltas.append(d)

    if not deltas:
        print(
            "ERR: no positive deltas (= every consecutive pair crossed a reset, or pct unchanged)",
            file=sys.stderr,
        )
        return 2

    avg = sum(deltas) / len(deltas)
    print(f"sample_size={len(deltas)} pairs from {len(recent)} entries")
    print(f"window_start={recent[0].get('datetime', '?')}")
    print(f"window_end  ={recent[-1].get('datetime', '?')}")
    print(f"avg_delta=+{avg:.2f}% min=+{min(deltas):.2f}% max=+{max(deltas):.2f}%")

    if avg < 4:
        verdict = "OK (= baseline ~+1-2% range)"
    elif avg > 8:
        verdict = "PENALTY (= ~+13% range, regression!)"
    else:
        verdict = "AMBIGUOUS (= 4-8% range, investigate)"
    print(f"verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
