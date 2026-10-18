"""研究步骤 25：锚点涨幅阈值扫描 —— 收紧多少最合适。

在 2021-09-17 ~ 2026-09-17 五年窗口上，把「锚点日涨幅下限」从 4% 逐步收紧到 10%，
比较每个档位的：信号数、信号精度、事件级精度、召回率、漏掉率、每日候选数。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.patterns import (  # noqa: E402
    FEATURE_KEYS,
    MineConfig,
    SecondBoardConfig,
    mine_stock,
    structure_reasons,
)
from stock_agent.screener.scanner import default_store_path  # noqa: E402
from stock_agent.screener.store import ScreenerStore  # noqa: E402

ROOT = _p
META = os.path.join(ROOT, ".data", "meta.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-start", default="2021-09-17")
    ap.add_argument("--window-end", default="2026-09-17")
    args = ap.parse_args()

    store = ScreenerStore(default_store_path())
    names: dict = {}
    shares: dict = {}
    if os.path.exists(META):
        meta = json.load(open(META, encoding="utf-8"))
        for c, v in meta.items():
            names[c] = v.get("name", "")
            shares[c] = v.get("float_shares", 0.0)

    with store._connect() as conn:
        conn.row_factory = None
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_bars WHERE adjust='bfq' ORDER BY code")]
    codes = [c for c in codes if c.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))]
    print(f"股票池: {len(codes)} 只  窗口 {args.window_start} ~ {args.window_end}\n")

    cfg = SecondBoardConfig()
    mine_cfg = MineConfig(
        anchor_min_gain=4.0,            # 从 4% 起挖全量
        anchor_max_gain=cfg.anchor_max_gain,
        min_gap=cfg.min_gap, max_gap=cfg.max_gap, max_abs_dev=0.10,
    )

    thresholds = [4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 9.5, 10.0]
    stats = {t: {"events": set(), "lu": set(), "flagged": set(),
                 "sig": 0, "hit": 0, "days": Counter()} for t in thresholds}

    for code in codes:
        bars = store.get_bars(code, "bfq")
        bars = [b for b in bars if b.get("date", "") >= args.window_start[:4]]
        bars = [b for b in bars if b.get("date", "") <= args.window_end]
        if len(bars) < 80:
            continue
        recs = mine_stock(bars, code, names.get(code, ""), mine_cfg,
                          float_shares=shares.get(code, 0.0))
        for r in recs:
            d = r.get("decision_date") or ""
            if d < args.window_start or d > args.window_end:
                continue
            g = float(r.get("anchor_gain") or 0)
            ev = (r["code"], r["board_date"])
            feats = {k: r.get(k) for k in FEATURE_KEYS if k in r}
            bad = bool(structure_reasons(feats, cfg))
            is_lu = bool(r.get("label_3d"))
            for t in thresholds:
                if g < t - 1e-9:
                    continue
                st = stats[t]
                st["events"].add(ev)
                if is_lu:
                    st["lu"].add(ev)
                if not bad:
                    st["sig"] += 1
                    st["flagged"].add(ev)
                    st["days"][d] += 1
                    if is_lu:
                        st["hit"] += 1

    hdr = (f"{'锚点≥':>7} {'事件数':>9} {'涨停波':>9} {'基准率':>8} "
           f"{'信号':>9} {'信号精度':>9} {'事件精度':>9} {'召回':>8} {'漏掉率':>8} {'每日候选':>9}")
    print(hdr)
    print("-" * 95)
    for t in thresholds:
        st = stats[t]
        ne = len(st["events"])
        nlu = len(st["lu"])
        nfl = len(st["flagged"])
        rec = len(st["flagged"] & st["lu"])
        sig = st["sig"]
        hit = st["hit"]
        nd = len(st["days"])
        print(f"{t:>6.1f}% {ne:>9,} {nlu:>9,} {nlu/max(ne,1):>8.1%} "
              f"{sig:>9,} {hit/max(sig,1):>9.1%} {rec/max(nfl,1):>9.1%} "
              f"{rec/max(nlu,1):>8.1%} {(nlu-rec)/max(nlu,1):>8.1%} {sig/max(nd,1):>9.1f}")
    print("-" * 95)
    print("基准率 = 该档位所有锚点事件中最终涨停的比例（闭眼挑的命中率）")
    print("事件精度 = 策略选中的事件里最终涨停的比例（> 基准率 才说明过滤有效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
