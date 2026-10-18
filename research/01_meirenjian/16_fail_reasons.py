"""研究步骤 16：美人肩「二次涨停失败」原因分析。

思路：先套用当前策略的结构过滤（得到"策略会选中的池子"），
再看池内哪些特征能区分「次日二次涨停成功」与「失败」。

用法::

    python research/01_meirenjian/16_fail_reasons.py --csv research/samples.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Dict, List, Tuple

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.patterns import (  # noqa: E402
    FEATURE_KEYS,
    SecondBoardConfig,
    structure_reasons,
)

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

EXTRA = ("decision_date", "fwd_ret", "fwd_min_20d", "fwd_ret_5d")

NEW_KEYS = (
    "turnover_last", "turnover_cons", "turnover_board",
    "vol_ratio_5d", "vol_ratio_10d", "vol_peak_ratio", "vol_peak_pos", "vol_decay",
    "hist_decline", "hist_decline_days",
    "amp_last", "upper_shadow_last", "close_pos_day",
    "bear_ratio", "big_loss_days", "amount_peak_ratio",
)

BASE_KEYS = (
    "gap", "boards_60d", "vol_ratio", "vol_trend", "vol_last_ratio",
    "board_vol_ratio", "avg_amp", "price", "hist_n", "ma_spread",
)


def load(path: str, since: str):
    keys = list(dict.fromkeys(list(FEATURE_KEYS) + list(EXTRA)))
    buf: Dict[str, List[float]] = {k: [] for k in keys}
    dates: List[str] = []
    labels: List[int] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            d = row.get("decision_date") or ""
            if since and d < since:
                continue
            dates.append(d)
            labels.append(int(float(row.get("label_next") or 0)))
            for k in keys:
                v = row.get(k)
                try:
                    buf[k].append(float(v) if v not in (None, "", "None") else float("nan"))
                except ValueError:
                    buf[k].append(float("nan"))
    return ({k: np.asarray(v, float) for k, v in buf.items()},
            np.asarray(labels, int), np.asarray(dates))


def auc(x, y) -> float:
    m = ~np.isnan(x)
    xx, yy = x[m], y[m]
    n1 = int(yy.sum())
    n0 = len(yy) - n1
    if n1 == 0 or n0 == 0:
        return 0.5
    o = np.argsort(xx, kind="mergesort")
    r = np.empty(len(xx), float)
    r[o] = np.arange(1, len(xx) + 1, dtype=float)
    return float((r[yy == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def q(x, p):
    v = x[~np.isnan(x)]
    return float(np.percentile(v, p)) if v.size else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples.csv"))
    ap.add_argument("--since", default="2019-01-01")
    args = ap.parse_args()
    if np is None:
        return 1

    cols, y, dates = load(args.csv, args.since)
    cfg = SecondBoardConfig()

    # ---- 套用当前策略的结构过滤（宽口径：不限震幅、不限涨停次数，只看"形状"）----
    loose = SecondBoardConfig(min_boards_60d=0, close_band=0.0, min_gap=2, max_gap=12,
                              min_vol_trend=0.0, min_vol_ratio=0.0, max_vol_ratio=999,
                              min_price=0, max_price=99999, min_avg_amp=0, min_hist_n=-9)
    n = len(y)
    pool = np.zeros(n, bool)
    keys = list(dict.fromkeys(list(FEATURE_KEYS)))
    for idx in range(n):
        feats = {k: (None if np.isnan(cols[k][idx]) else float(cols[k][idx])) for k in keys}
        if not structure_reasons(feats, loose):
            pool[idx] = True

    print(f"样本 {n}   形状通过 {int(pool.sum())} ({pool.mean():.1%})")
    base = float(y[pool].mean())
    print(f"池内基准命中率（次日二次涨停）: {base:.2%}\n")

    # ---- 池内特征区分度 ----
    rows = []
    for k in list(NEW_KEYS) + list(BASE_KEYS):
        a = auc(cols[k][pool], y[pool])
        rows.append((abs(a - 0.5), k, a))
    rows.sort(reverse=True)

    print("=" * 118)
    print("池内特征区分度（成功 vs 失败）")
    print("=" * 118)
    print(f"{'特征':<20}{'AUC':>7}{'方向':>10} | {'失败组 p25/p50/p75':>28} | "
          f"{'成功组 p25/p50/p75':>28} | {'差异':>9}")
    print("=" * 118)
    for strength, k, a in rows[:26]:
        x = cols[k]
        fail, succ = pool & (y == 0), pool & (y == 1)
        f = (q(x[fail], 25), q(x[fail], 50), q(x[fail], 75))
        s_ = (q(x[succ], 25), q(x[succ], 50), q(x[succ], 75))
        print(f"{k:<20}{a:>7.3f}{'越大越好' if a >= 0.5 else '越小越好':>10} | "
              f"{f[0]:>9.3f}{f[1]:>9.3f}{f[2]:>9.3f} | "
              f"{s_[0]:>9.3f}{s_[1]:>9.3f}{s_[2]:>9.3f} | {s_[1] - f[1]:>+9.3f}")
    print("=" * 118)

    # ---- 关键特征分档命中率 ----
    print("\n关键特征分五档 → 池内命中率（失败诱因一目了然）")
    for k in ("turnover_last", "turnover_cons", "vol_ratio_5d", "vol_peak_ratio",
              "vol_peak_pos", "vol_decay", "hist_decline", "hist_decline_days",
              "upper_shadow_last", "close_pos_day", "bear_ratio", "big_loss_days",
              "board_vol_ratio", "vol_trend", "amount_peak_ratio", "amp_last"):
        x = cols[k]
        v = x[pool]
        v = v[~np.isnan(v)]
        if v.size < 500:
            continue
        edges = np.percentile(v, [0, 20, 40, 60, 80, 100])
        line = f"  {k:<20}"
        for i in range(5):
            lo, hi = edges[i], edges[i + 1]
            m = pool & (x >= lo) & ((x <= hi) if i == 4 else (x < hi))
            if m.sum() < 50:
                line += f"{'-':>11}"
                continue
            line += f"{y[m].mean():>7.1%}/{q(x[m], 50):>6.2f}"
        print(line + f"   (基准{base:.1%})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
