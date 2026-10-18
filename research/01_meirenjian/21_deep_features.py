"""研究步骤 21：加入深层特征（筹码/市值/周K/换手）后的成功 vs 失败分析。

数据：2019-2026（6.7 年）深交所 1289 只，166 万决策点 / 22.2 万锚点事件。

用法::

    python research/01_meirenjian/21_deep_features.py --csv research/samples6.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Any, Dict, List

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

KEYS = [
    "code", "board_date", "decision_date", "label_3d", "label_next",
    "anchor_gain", "anchor_is_lu", "gap", "range_pct", "close_pos",
    # 深层新特征
    "chip_pct60", "cost_dev60", "cost_std60", "chip_pct120", "cost_dev120",
    "float_mv_yi", "ln_float_mv", "turnover60",
    "week_ma_bull", "week_hist_n", "week_ret_4w",
    # 已有特征
    "boards_60d", "vol_trend", "vol_decay", "vol_last_ratio", "vol_ratio_5d",
    "board_vol_ratio", "hist_decline", "hist_decline_days", "hist_n",
    "amp_last", "avg_amp", "turnover_last", "turnover_cons", "turnover_board",
    "price", "ret_20d", "ret_60d", "dist_ma20", "ma_spread", "close_vs_hh60",
    "bear_ratio", "big_loss_days", "fwd_ret", "fwd_ret_3d", "fwd_min_3d", "fwd_max_3d",
]

NEW = ("chip_pct60", "cost_dev60", "cost_std60", "chip_pct120", "cost_dev120",
       "float_mv_yi", "ln_float_mv", "turnover60", "week_ma_bull", "week_hist_n",
       "week_ret_4w")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples6.csv"))
    ap.add_argument("--since", default="2019-01-01")
    args = ap.parse_args()
    if np is None:
        return 1

    best: Dict[tuple, list] = {}
    n = 0
    with open(args.csv, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        hdr = next(rd)
        ix = {k: i for i, k in enumerate(hdr)}
        global KEYS
        miss = [k for k in KEYS if k not in ix]
        if miss:
            print(f"（跳过不存在的列: {miss}）")
            KEYS = [k for k in KEYS if k in ix]
        cols = [ix[k] for k in KEYS]
        gi = KEYS.index("gap")
        for r in rd:
            if r[ix["decision_date"]] < args.since:
                continue
            n += 1
            key = (r[ix["code"]], r[ix["board_date"]])
            try:
                g = float(r[ix["gap"]])
            except ValueError:
                continue
            cur = best.get(key)
            if cur is None or g > cur[0]:
                best[key] = (g, [r[c] for c in cols])
    print(f"决策点 {n:,}  →  锚点事件 {len(best):,}\n")

    def arr(k: str) -> np.ndarray:
        j = KEYS.index(k)
        out = []
        for _, row in best.values():
            v = row[j]
            try:
                out.append(float(v) if v not in ("", "None") else np.nan)
            except ValueError:
                out.append(np.nan)
        return np.asarray(out, float)

    A = {k: arr(k) for k in KEYS}
    succ = A["label_3d"] > 0
    lu = A["anchor_is_lu"] > 0
    print(f"整体：事件 {len(succ):,}  成功 {int(succ.sum()):,} ({succ.mean():.1%})")
    print(f"  涨停锚点 {int(lu.sum()):,} 只  成功率 {succ[lu].mean():.1%}")
    print(f"  温和锚点 {int((~lu).sum()):,} 只  成功率 {succ[~lu].mean():.1%}")

    for name, m in (("涨停锚点", lu), ("温和锚点", ~lu)):
        print()
        print("=" * 112)
        print(f"【{name}】成功 vs 失败 特征对比（中位数）  基准 {succ[m].mean():.1%}")
        print("=" * 112)
        print(f"{'特征':<20}{'失败组':>12}{'成功组':>12}{'差距':>10}{'方向':>14}{'新?':>5}")
        rows = []
        for k in KEYS:
            if k in ("code", "board_date", "decision_date", "label_3d", "label_next"):
                continue
            x = A[k]
            fm = np.nanmedian(x[m & ~succ])
            sm = np.nanmedian(x[m & succ])
            if np.isnan(fm) or np.isnan(sm) or (fm == 0 and sm == 0):
                continue
            ratio = (sm / fm) if fm else float("inf")
            rows.append((abs(np.log(ratio)) if np.isfinite(ratio) and ratio > 0 else 0,
                         k, fm, sm, ratio))
        for _, k, fm, sm, ratio in sorted(rows, reverse=True)[:22]:
            rtxt = f"{ratio:.2f}x" if np.isfinite(ratio) and ratio > 0 else "-"
            print(f"{k:<20}{fm:>12.4f}{sm:>12.4f}{rtxt:>10}"
                  f"{'成功组更高' if sm > fm else '成功组更低':>14}"
                  f"{'  ★' if k in NEW else '':>5}")

    # ---- 深层特征分档 ----
    print()
    print("=" * 112)
    print("【深层特征分档 → 成功率】（全部锚点）")
    print("=" * 112)
    for k in NEW:
        x = A[k]
        v = x[~np.isnan(x)]
        if v.size < 2000:
            continue
        edges = np.percentile(v, [0, 20, 40, 60, 80, 100])
        line = f"  {k:<16}"
        for i in range(5):
            lo, hi = edges[i], edges[i + 1]
            mm = (x >= lo) & ((x <= hi) if i == 4 else (x < hi))
            line += f"{succ[mm].mean():>9.1%}" if mm.sum() >= 200 else f"{'-':>9}"
        print(line + f"   中位区间 {edges[0]:.3f}~{edges[-1]:.3f}")

    # ---- 组合规则 ----
    print()
    print("=" * 112)
    print("【加入深层特征后的过滤规则】")
    print("=" * 112)
    def ev(name, conds):
        m = np.ones(len(succ), bool)
        for k, op, t in conds:
            x = A[k]
            m &= (x >= t) if op == ">=" else (x <= t)
        m &= ~np.isnan(A["fwd_ret"])
        if m.sum() < 30:
            print(f"{name:<46}{int(m.sum()):>9}   样本不足")
            return
        r = succ[m].mean()
        print(f"{name:<46}{int(m.sum()):>9,}{r:>9.2%}{r / succ.mean():>8.2f}x"
              f"{np.nanmean(A['fwd_ret'][m]):>10.2%}{np.nanmean(A['fwd_min_3d'][m]):>10.2%}")

    print(f"{'规则':<46}{'事件数':>9}{'成功率':>9}{'提升':>8}{'次日收益':>10}{'3日回撤':>10}")
    base = [("gap", ">=", 3), ("gap", "<=", 10), ("range_pct", "<=", 0.12)]
    ev("A 形态(整理3~10天+振幅≤12%)", base)
    ev("B A+周线多头", base + [("week_ma_bull", ">=", 1)])
    ev("C A+筹码集中(cost_std60≤0.15)", base + [("cost_std60", "<=", 0.15)])
    ev("D A+获利盘不过高(chip_pct60≤0.8)", base + [("chip_pct60", "<=", 0.8)])
    ev("E A+60日均换手≥2%", base + [("turnover60", ">=", 2.0)])
    ev("F A+流通市值≥30亿", base + [("float_mv_yi", ">=", 30.0)])
    ev("G B+C+D 周线+筹码+获利盘", base + [("week_ma_bull", ">=", 1),
                                            ("cost_std60", "<=", 0.15),
                                            ("chip_pct60", "<=", 0.8)])
    ev("H G+E 再加换手", base + [("week_ma_bull", ">=", 1), ("cost_std60", "<=", 0.15),
                                  ("chip_pct60", "<=", 0.8), ("turnover60", ">=", 2.0)])
    ev("I H + 原六条规避(MACD柱等)", base + [("week_ma_bull", ">=", 1), ("cost_std60", "<=", 0.15),
                                              ("chip_pct60", "<=", 0.8), ("turnover60", ">=", 2.0),
                                              ("hist_decline", "<=", 0.35),
                                              ("vol_trend", ">=", 0.6)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
