"""研究步骤 10：挖掘「趋势票（涨不停不涨停）」样本，导出特征 CSV。

用法::

    python research/04_trend/10_mine_trend.py --window 20 --stride 2 --out research/trend_samples.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Any, Dict, List

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.scanner import default_store_path  # noqa: E402
from stock_agent.screener.store import ScreenerStore  # noqa: E402
from stock_agent.screener.trend import (  # noqa: E402
    TREND_FEATURE_KEYS,
    TREND_META_KEYS,
    MineTrendConfig,
    mine_trend,
)

ROOT = _p
NAMES = os.path.join(ROOT, ".data", "names.json")


def is_bj(code: str) -> bool:
    return code.startswith(("4", "8", "920"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adjust", default="qfq")
    ap.add_argument("--window", type=int, default=20, help="观察窗口（交易日）")
    ap.add_argument("--stride", type=int, default=2, help="每 N 天取一个样本（降采样）")
    ap.add_argument("--max-single-gain", type=float, default=10.5)
    ap.add_argument("--min-up-ratio", type=float, default=0.55)
    ap.add_argument("--min-bars", type=int, default=200)
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--market", default="main",
                    choices=["all", "main", "sz", "sh", "zb", "sszb"])
    ap.add_argument("--out", default=os.path.join(ROOT, "research", "trend_samples.csv"))
    args = ap.parse_args()

    store = ScreenerStore(default_store_path())
    names: Dict[str, str] = {}
    if os.path.exists(NAMES):
        names = json.load(open(NAMES, encoding="utf-8"))

    def keep(code: str) -> bool:
        if is_bj(code):
            return False
        if args.market == "main":
            return code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))
        if args.market == "sz":
            return code.startswith(("000", "001", "002", "003", "300", "301"))
        if args.market == "sh":
            return code.startswith(("600", "601", "603", "605", "688", "689"))
        if args.market == "zb":
            return code.startswith(("000", "001", "002", "003"))
        if args.market == "sszb":
            return code.startswith(("600", "601", "603", "605"))
        return True

    with store._connect() as conn:
        conn.row_factory = None
        codes = [
            r[0]
            for r in conn.execute(
                "SELECT code FROM fetch_state WHERE adjust=? AND bars>=? ORDER BY code",
                (args.adjust, args.min_bars),
            )
        ]
    codes = [c for c in codes if keep(c)]
    if args.limit:
        codes = codes[: args.limit]
    print(f"参与挖掘: {len(codes)} 只  窗口={args.window} 天  降采样每 {args.stride} 天")

    cfg = MineTrendConfig(window=args.window, max_single_gain=args.max_single_gain,
                          min_up_ratio=args.min_up_ratio)
    fields = list(TREND_META_KEYS) + [k for k in TREND_FEATURE_KEYS if k not in TREND_META_KEYS]
    n_samples = 0
    t0 = time.time()

    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for start in range(0, len(codes), args.batch):
            batch = codes[start : start + args.batch]
            bars_map = store.get_many(batch, args.adjust)
            for code in batch:
                bars = bars_map.get(code) or []
                if len(bars) < args.min_bars:
                    continue
                recs = mine_trend(bars, code, names.get(code, ""), cfg)
                for i, r in enumerate(recs):
                    if args.stride > 1 and i % args.stride:
                        continue
                    writer.writerow(r)
                    n_samples += 1
            done = min(start + args.batch, len(codes))
            print(f"\r  已处理 {done}/{len(codes)}  样本 {n_samples}", end="", flush=True)
        print()

    print(f"\n挖掘完成，用时 {time.time() - t0:.0f}s   样本 {n_samples}")
    print(f"输出: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
