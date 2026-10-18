"""研究步骤 19：广义美人肩（温和锚点）规律分析。

锚点 = 涨幅 ≥ 3% 的强势阳线（不要求涨停）。
本脚本把锚点分成「温和 (3%~9.5%)」与「涨停 (≥9.5%)」两档，
分别统计整理天数 / 震幅 / 特征区分度，找出各自的规律。

用法::

    python research/01_meirenjian/19_mild_anchor.py --csv research/samples.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Dict, List

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
    "code", "board_date", "decision_date", "label_next", "label_3d",
    "anchor_gain", "anchor_is_lu", "gap",
    "dev_close_min", "dev_close_max", "dev_high_max", "dev_low_min", "range_pct",
    "close_pos", "avg_amp", "max_amp", "up_days",
    "vol_ratio", "vol_last_ratio", "vol_trend", "board_vol_ratio",
    "vol_decay", "vol_ratio_5d", "turnover_last", "turnover_cons",
    "boards_60d", "ma_bull", "ma_spread", "close_vs_ma5", "close_vs_ma20", "ma20_slope",
    "hist_n", "hist_decline", "hist_decline_days", "hist_pos_ratio",
    "amp_last", "upper_shadow_last", "close_pos_day", "bear_ratio", "big_loss_days",
    "price", "ret_5d", "ret_20d", "ret_pre_board", "dist_ma20", "close_vs_hh60",
    "fwd_ret", "fwd_ret_3d", "fwd_max_3d", "fwd_min_3d",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples.csv"))
    ap.add_argument("--since", default="2023-09-01")
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
        missing = [k for k in KEYS if k not in ix]
        if missing:
            print(f"（CSV 中不存在的列已跳过: {missing}）")
            KEYS = [k for k in KEYS if k in ix]
        cols = [ix[k] for k in KEYS]
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
    mild = (A["anchor_gain"] >= 3.0) & (A["anchor_gain"] < 9.5)
    lu = A["anchor_gain"] >= 9.5

    print("=" * 104)
    print("【一】两类锚点的基本规律")
    print("=" * 104)
    for name, m in (("温和锚点 3~9.5%", mild), ("涨停锚点 ≥9.5%", lu)):
        print(f"\n  {name}   事件 {int(m.sum()):,}   基准成功率 {succ[m].mean():.2%}")
        print(f"    {'整理天数':<10}" + "".join(f"{g:>9}天" for g in (3, 4, 5, 6, 8, 10, 12, 15, 20)))
        line = f"    {'成功率':<10}"
        for g in (3, 4, 5, 6, 8, 10, 12, 15, 20):
            mm = m & (A["gap"] == g)
            line += f"{succ[mm].mean():>10.1%}" if mm.sum() >= 100 else f"{'-':>10}"
        print(line)

        print(f"    {'震幅 range_pct':<10}" + "".join(f"{x:>9}" for x in ("<6%", "6-8%", "8-10%", "10-12%", "12-15%", ">15%")))
        line = f"    {'成功率':<10}"
        for lo, hi in ((0, 0.06), (0.06, 0.08), (0.08, 0.10), (0.10, 0.12), (0.12, 0.15), (0.15, 9)):
            mm = m & (A["range_pct"] >= lo) & (A["range_pct"] < hi)
            line += f"{succ[mm].mean():>10.1%}" if mm.sum() >= 100 else f"{'-':>10}"
        print(line)

    print()
    print("=" * 104)
    print("【二】温和锚点：成功 vs 失败 特征对比（中位数）")
    print("=" * 104)
    cmp_keys = ["gap", "range_pct", "dev_close_min", "dev_close_max", "close_pos",
                "boards_60d", "vol_trend", "vol_decay", "vol_last_ratio", "vol_ratio_5d",
                "board_vol_ratio", "hist_decline", "hist_decline_days", "hist_n",
                "amp_last", "avg_amp", "turnover_last", "turnover_cons",
                "price", "ret_pre_board", "ret_20d", "dist_ma20", "ma_spread",
                "bear_ratio", "big_loss_days", "close_vs_hh60", "up_days"]
    print(f"{'特征':<20}{'失败组':>12}{'成功组':>12}{'差距':>12}{'方向':>14}")
    print("-" * 104)
    rows = []
    for k in cmp_keys:
        x = A[k]
        fm = np.nanmedian(x[mild & ~succ])
        sm = np.nanmedian(x[mild & succ])
        if np.isnan(fm) or np.isnan(sm):
            continue
        ratio = sm / fm if fm else float("inf")
        rows.append((abs(np.log(ratio)) if np.isfinite(ratio) and ratio > 0 else 0, k, fm, sm, ratio))
    for _, k, fm, sm, ratio in sorted(rows, reverse=True):
        rtxt = f"{ratio:.2f}x" if np.isfinite(ratio) and ratio > 0 else "-"
        print(f"{k:<20}{fm:>12.4f}{sm:>12.4f}{rtxt:>12}"
              f"{'成功组更高' if sm > fm else '成功组更低':>14}")
    print("=" * 104)

    # ---- 温和锚点的过滤规则 ----
    print()
    print("=" * 104)
    print("【三】温和锚点分支：过滤规则效果（2023-09 ~ 2026-09）")
    print("=" * 104)
    base = succ[mild].mean()
    rules = [
        ("A 无过滤", []),
        ("B 整理3~10天", [("gap", ">=", 3), ("gap", "<=", 10)]),
        ("C B+震幅≤12%", [("gap", ">=", 3), ("gap", "<=", 10), ("range_pct", "<=", 0.12)]),
        ("D C+60日涨停≥1", [("gap", ">=", 3), ("gap", "<=", 10), ("range_pct", "<=", 0.12),
                            ("boards_60d", ">=", 1)]),
        ("E D+量能趋势≥0.6", [("gap", ">=", 3), ("gap", "<=", 10), ("range_pct", "<=", 0.12),
                               ("boards_60d", ">=", 1), ("vol_trend", ">=", 0.6)]),
        ("F E+MACD柱回落≤30%", [("gap", ">=", 3), ("gap", "<=", 10), ("range_pct", "<=", 0.12),
                                 ("boards_60d", ">=", 1), ("vol_trend", ">=", 0.6),
                                 ("hist_decline", "<=", 0.30)]),
        ("G F+首板放量≤2.2", [("gap", ">=", 3), ("gap", "<=", 10), ("range_pct", "<=", 0.12),
                               ("boards_60d", ">=", 1), ("vol_trend", ">=", 0.6),
                               ("hist_decline", "<=", 0.30), ("board_vol_ratio", "<=", 2.2)]),
        ("H G+收盘位置≥0.4", [("gap", ">=", 3), ("gap", "<=", 10), ("range_pct", "<=", 0.12),
                               ("boards_60d", ">=", 1), ("vol_trend", ">=", 0.6),
                               ("hist_decline", "<=", 0.30), ("board_vol_ratio", "<=", 2.2),
                               ("close_pos", ">=", 0.4)]),
    ]
    print(f"{'规则':<28}{'事件数':>10}{'成功数':>9}{'成功率':>9}{'提升':>8}{'次日收益':>11}")
    for name, conds in rules:
        m = mild.copy()
        for k, op, t in conds:
            x = A[k]
            m &= (x >= t) if op == ">=" else (x <= t)
        if m.sum() < 30:
            print(f"{name:<28}{int(m.sum()):>10}   样本不足")
            continue
        r = succ[m].mean()
        print(f"{name:<28}{int(m.sum()):>10,}{int(succ[m].sum()):>9,}{r:>9.2%}"
              f"{r / base:>7.2f}x{np.nanmean(A['fwd_ret'][m]):>10.2%}")
    print(f"{'（温和锚点基准）':<28}{int(mild.sum()):>10,}{int(succ[mild].sum()):>9,}"
          f"{base:>9.2%}{1.0:>7.2f}x{np.nanmean(A['fwd_ret'][mild]):>10.2%}")
    print("=" * 104)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
