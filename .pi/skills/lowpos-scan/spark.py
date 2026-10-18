"""低位票的终端字符图（K 线 + 均线粘合可视化）。

用法::

    python .pi/skills/lowpos-scan/spark.py 605366 002990 600835
    python .pi/skills/lowpos-scan/spark.py 605366 --days 90 --height 16

在终端打印最近 N 天的收盘价走势，并把 MA5 / MA10 / MA20 叠加在同一张图上：

* ``#`` 收盘价
* ``'`` MA5
* ``:`` MA10
* ``.`` MA20

均线粘合时三种符号会叠成一条窄带；随后 ``#`` 向上分离 = 拐头。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DB = os.path.join(ROOT, ".data", "screener.db")
NAMES = os.path.join(ROOT, ".data", "names.json")


def _ma(xs: List[Optional[float]], w: int, i: int) -> Optional[float]:
    if i + 1 < w:
        return None
    seg = [v for v in xs[i + 1 - w: i + 1] if v is not None]
    return sum(seg) / len(seg) if len(seg) == w else None


def spark(code: str, name: str, bars: list, days: int, height: int) -> None:
    closes_all = [b["close"] for b in bars]
    highs_all = [b["high"] for b in bars]
    lows_all = [b["low"] for b in bars]
    n_all = len(bars)
    m5_all = [_ma(closes_all, 5, i) for i in range(n_all)]
    m10_all = [_ma(closes_all, 10, i) for i in range(n_all)]
    m20_all = [_ma(closes_all, 20, i) for i in range(n_all)]

    off = max(0, n_all - days)
    dates = [b["date"] for b in bars[off:]]
    closes = closes_all[off:]
    lows = lows_all[off:]
    highs = highs_all[off:]
    m5, m10, m20 = m5_all[off:], m10_all[off:], m20_all[off:]
    n = len(closes)

    lo = min(v for v in lows if v is not None)
    hi = max(v for v in highs if v is not None)
    if hi <= lo:
        hi = lo + 1e-9

    def row(v):
        if v is None:
            return None
        return max(0, min(height - 1, int(round((v - lo) / (hi - lo) * (height - 1)))))

    grid = [[" "] * n for _ in range(height)]
    for i in range(n):
        for series, ch in ((m20, "."), (m10, ":"), (m5, "'")):
            r = row(series[i])
            if r is not None:
                grid[r][i] = ch
        r = row(closes[i])
        if r is not None:
            grid[r][i] = "#"

    print()
    print(f"  {code} {name}  最新 {closes[-1]:.2f}  区间 [{lo:.2f}, {hi:.2f}]  最近 {n} 天")
    for r in range(height - 1, -1, -1):
        price = lo + (hi - lo) * r / (height - 1)
        print(f"  {price:8.2f} │ " + "".join(grid[r]))
    print("           └" + "─" * n)
    print(f"            {dates[0]}  →  {dates[-1]}     （# 收盘 · ' MA5 · : MA10 · . MA20）")
    vals = [m5[-1], m10[-1], m20[-1]]
    spread = None
    if all(v is not None for v in vals):
        avg = sum(vals) / 3
        spread = (max(vals) - min(vals)) / avg if avg else None
    print(f"            均线: MA5 {m5[-1] or 0:.2f} / MA10 {m10[-1] or 0:.2f} / MA20 {m20[-1] or 0:.2f}"
          + (f"   三线离散 {spread * 100:.2f}%" if spread is not None else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="低位票终端字符图")
    ap.add_argument("codes", nargs="+", help="股票代码（如 605366 002990）")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--height", type=int, default=14)
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    names = {}
    if os.path.exists(NAMES):
        try:
            names = json.load(open(NAMES, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            names = {}

    conn = sqlite3.connect(args.db)
    for code in args.codes:
        rows = conn.execute(
            "SELECT date, open, high, low, close FROM daily_bars "
            "WHERE code=? AND adjust='qfq' ORDER BY date DESC LIMIT ?",
            (code, args.days + 25),
        ).fetchall()
        if not rows:
            print(f"\n  {code}: 本地缓存没有数据（先跑 warmup）")
            continue
        bars = [
            {"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
            for r in reversed(rows)
        ]
        spark(code, names.get(code, ""), bars, args.days, args.height)
    return 0


if __name__ == "__main__":
    sys.exit(main())
