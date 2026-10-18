"""研究步骤 2：挖掘全部「首板 → 整理 → 二次涨停」样本，导出特征 CSV。

用法::

    python research/01_meirenjian/02_mine.py --adjust qfq --out research/samples.csv
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

from stock_agent.screener.patterns import (  # noqa: E402
    FEATURE_KEYS,
    META_KEYS,
    MineConfig,
    count_events,
    mine_stock,
)
from stock_agent.screener.scanner import default_store_path  # noqa: E402
from stock_agent.screener.store import ScreenerStore  # noqa: E402

ROOT = _p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adjust", default="qfq")
    ap.add_argument("--min-bars", type=int, default=200, help="至少要有多少根日线才参与挖掘")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(ROOT, "research", "samples.csv"))
    ap.add_argument("--no-context", action="store_true", help="跳过大盘/板块上下文特征")
    ap.add_argument("--min-gap", type=int, default=2)
    ap.add_argument("--max-gap", type=int, default=12)
    ap.add_argument("--max-abs-dev", type=float, default=0.10)
    args = ap.parse_args()

    store = ScreenerStore(default_store_path())
    names: Dict[str, str] = {}
    names_file = os.path.join(ROOT, ".data", "names.json")
    if os.path.exists(names_file):
        names = json.load(open(names_file, encoding="utf-8"))
    shares: Dict[str, float] = {}
    meta_file = os.path.join(ROOT, ".data", "meta.json")
    if os.path.exists(meta_file):
        meta = json.load(open(meta_file, encoding="utf-8"))
        shares = {k: (v.get("float_shares") or 0.0) for k, v in meta.items()}

    with store._connect() as conn:
        conn.row_factory = None
        codes = [
            r[0] for r in conn.execute(
                "SELECT code FROM fetch_state WHERE adjust=? AND bars>=? ORDER BY code",
                (args.adjust, args.min_bars),
            )
        ]
    if args.limit:
        codes = codes[: args.limit]
    print(f"参与挖掘的股票: {len(codes)} 只")

    cfg = MineConfig(min_gap=args.min_gap, max_gap=args.max_gap, max_abs_dev=args.max_abs_dev)
    mctx = None
    if not args.no_context:
        from stock_agent.screener.context import get_context

        print("加载大盘/板块上下文 ...")
        mctx = get_context(store)
        print(f"  大盘 {len(mctx.market)} 天 / 板块 {len(mctx.sector)} 个")
    fields = list(META_KEYS) + [k for k in FEATURE_KEYS if k not in META_KEYS]
    n_samples = n_pos = 0
    events: set = set()
    pos_events: set = set()
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
                for r in mine_stock(bars, code, names.get(code, ""), cfg,
                                    float_shares=shares.get(code, 0.0), mctx=mctx):
                    writer.writerow(r)
                    n_samples += 1
                    key = (r["code"], r["board_date"])
                    events.add(key)
                    if r["label_next"]:
                        n_pos += 1
                        pos_events.add(key)
            done = min(start + args.batch, len(codes))
            print(f"\r  已处理 {done}/{len(codes)}  样本 {n_samples}", end="", flush=True)
        print()

    print(f"\n挖掘完成，用时 {time.time() - t0:.0f}s")
    print(f"  决策点样本数       : {n_samples}")
    print(f"  正样本(次日涨停)   : {n_pos}  ({n_pos / max(n_samples, 1):.2%})")
    print(f"  首板事件数         : {len(events)}")
    print(f"  产生过二次涨停的事件 : {len(pos_events)}"
          f"  ({len(pos_events) / max(len(events), 1):.1%})")
    print(f"  输出: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
