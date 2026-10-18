"""研究步骤 26：横盘幅度 × 量能 二维扫描（锚点已固定 ≥9.5%）。

在 2021-09-17 ~ 2026-09-17 五年窗口上，扫描两个杠杆：
  * close_band（横盘收盘震幅）：0.03 / 0.05 / 0.07 / 0.10
  * min_vol_trend（量能趋势 = 后2日均量/前2日均量）：0(关) / 0.8 / 1.0 / 1.2

其余沿用当前默认（总振幅≤15%、排除整理期已涨停、沪深主板）。
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
        anchor_min_gain=cfg.anchor_min_gain,   # 9.5
        anchor_max_gain=cfg.anchor_max_gain,
        min_gap=cfg.min_gap, max_gap=cfg.max_gap, max_abs_dev=0.10,
    )

    # 基础过滤：除 close_band / vol_trend 外全部用当前默认
    base_cfg = SecondBoardConfig()
    base_cfg.close_band = 0.0
    base_cfg.min_vol_trend = 0.0

    close_bands = [0.03, 0.05, 0.07, 0.10]
    vol_trends = [0.0, 0.8, 1.0, 1.2]
    combos = [(cb, vt) for cb in close_bands for vt in vol_trends]
    stats = {c: {"flagged": set(), "sig": 0, "hit": 0, "days": Counter()} for c in combos}

    events: set = set()
    lu_events: set = set()

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
            ev = (r["code"], r["board_date"])
            events.add(ev)
            is_lu = bool(r.get("label_3d"))
            if is_lu:
                lu_events.add(ev)
            feats = {k: r.get(k) for k in FEATURE_KEYS if k in r}
            if structure_reasons(feats, base_cfg):
                continue
            dcm = float(feats.get("dev_close_max") or 0)
            dcl = float(feats.get("dev_close_min") or 0)
            vt = float(feats.get("vol_trend") or 0)
            for cb, vtt in combos:
                if dcl < -cb or dcm > cb:
                    continue
                if vtt > 0 and vt < vtt:
                    continue
                st = stats[(cb, vtt)]
                st["sig"] += 1
                st["flagged"].add(ev)
                st["days"][d] += 1
                if is_lu:
                    st["hit"] += 1

    nlu = len(lu_events)
    print(f"锚点事件: {len(events):,}  涨停波: {nlu:,}  基准率 {nlu/max(len(events),1):.1%}\n")
    hdr = (f"{'close_band':>11} {'量能≥':>6} {'信号':>8} {'信号精度':>9} "
           f"{'事件精度':>9} {'召回':>8} {'漏掉率':>8} {'每日候选':>9}")
    print(hdr)
    print("-" * 84)
    for cb, vtt in combos:
        st = stats[(cb, vtt)]
        nfl = len(st["flagged"])
        rec = len(st["flagged"] & lu_events)
        nd = len(st["days"])
        mark = "  <- 当前" if (cb == 0.05 and vtt == 0.0) else ""
        print(f"{cb:>10.0%} {vtt:>6.1f} {st['sig']:>8,} {st['hit']/max(st['sig'],1):>9.1%} "
              f"{rec/max(nfl,1):>9.1%} {rec/max(nlu,1):>8.1%} "
              f"{(nlu-rec)/max(nlu,1):>8.1%} {st['sig']/max(nd,1):>9.1f}{mark}")
    print("-" * 84)
    print("事件精度 > 30.0% 才算「过滤产生正收益」；每日候选越少越好。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
