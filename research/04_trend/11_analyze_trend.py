"""研究步骤 11：趋势票特征区分度分析 + 规则挖掘。

用法::

    python research/04_trend/11_analyze_trend.py --csv research/trend_samples.csv
    python research/04_trend/11_analyze_trend.py --csv research/trend_samples.csv --greedy
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

from stock_agent.screener.trend import TREND_FEATURE_KEYS  # noqa: E402

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

EXTRA = ("date", "fwd_ret_5d", "fwd_ret_10d", "fwd_ret_20d",
         "fwd_max_20d", "fwd_min_20d", "fwd_limit_up_20d")

# 方向固定（业务含义明确）的特征；其余由数据决定
FORCE_DIR: Dict[str, bool] = {
    "up_ratio": True,
    "ma_bull": True,
    "ma_bull_ratio": True,
    "ma20_slope": True,
    "ma30_slope": True,
    "hist_n": True,
    "hist_pos_ratio": True,
    "vol_updown": True,
    "max_pullback": False,
    "ret_std": False,
    "bias5": False,
    "bias20": False,
    "macd_days_since_cross": False,
    "ret_60d": False,
    "ret_120d": False,
    "total_gain": False,
    "max_gain": False,
    "vol_stability": False,
}


def load(path: str, since: str):
    keys = list(dict.fromkeys(list(TREND_FEATURE_KEYS) + list(EXTRA)))
    buf: Dict[str, List[float]] = {k: [] for k in keys}
    dates: List[str] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            d = row.get("date") or ""
            if since and d < since:
                continue
            dates.append(d)
            for k in keys:
                v = row.get(k)
                try:
                    buf[k].append(float(v) if v not in (None, "", "None") else float("nan"))
                except ValueError:
                    buf[k].append(float("nan"))
    return {k: np.asarray(v, float) for k, v in buf.items()}, np.asarray(dates)


def auc(x, y) -> float:
    m = ~np.isnan(x) & ~np.isnan(y)
    xx, yy = x[m], y[m]
    n1 = int(yy.sum())
    n0 = len(yy) - n1
    if n1 == 0 or n0 == 0:
        return 0.5
    o = np.argsort(xx, kind="mergesort")
    r = np.empty(len(xx), float)
    r[o] = np.arange(1, len(xx) + 1, dtype=float)
    return float((r[yy == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "trend_samples.csv"))
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--split", default="2025-01-01")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--greedy", action="store_true")
    args = ap.parse_args()
    if np is None:
        return 1

    cols, dates = load(args.csv, args.since)
    train = dates < args.split
    test = ~train
    y = (cols["fwd_ret_20d"] > 0).astype(int)
    y = np.where(np.isnan(cols["fwd_ret_20d"]), np.nan, y)
    print(f"样本 {len(y)}  训练 {train.sum()}  测试 {test.sum()}")
    print(f"测试集基准：20日上涨概率 {np.nanmean(y[test]):.2%}  "
          f"平均20日收益 {np.nanmean(cols['fwd_ret_20d'][test]):+.2f}%  "
          f"平均最大回撤 {np.nanmean(cols['fwd_min_20d'][test]):+.2f}%\n")

    rows = []
    for k in TREND_FEATURE_KEYS:
        a = auc(cols[k][train], y[train])
        rows.append((abs(a - 0.5), k, a))
    rows.sort(reverse=True)

    print("=" * 104)
    print(f"{'特征':<22}{'AUC':>7}{'方向':>10} | "
          f"{'低分位':>26}{'高分位':>26} | {'20日均值差':>10}")
    print(f"{'':<22}{'':>7}{'':>10} | {'20日收益':>12}{'上涨率':>8}{'回撤':>6}"
          f"{'20日收益':>12}{'上涨率':>8}{'回撤':>6} |")
    print("=" * 104)
    for strength, k, a in rows[: args.top]:
        x = cols[k]
        qs = np.nanpercentile(x[train], [20, 80])
        lo = train & (x <= qs[0])
        hi = train & (x >= qs[1])
        if lo.sum() < 100 or hi.sum() < 100:
            continue

        def stat(m):
            return (np.nanmean(cols["fwd_ret_20d"][m]),
                    np.nanmean(y[m]),
                    np.nanmean(cols["fwd_min_20d"][m]))

        lr, lp, ld = stat(lo)
        hr, hp, hd = stat(hi)
        print(f"{k:<22}{a:>7.3f}{'越大越好' if a >= 0.5 else '越小越好':>10} | "
              f"{lr:>+11.2f}%{lp:>8.1%}{ld:>+6.1f}%"
              f"{hr:>+11.2f}%{hp:>8.1%}{hd:>+6.1f}% | {hr - lr:>+9.2f}%")
    print("=" * 104)

    # ---- 分位表：几个关键特征 ----
    print("\n关键特征分五档（训练集，20 日收益 / 上涨率）")
    for k in ("ma_bull_ratio", "hist_pos_ratio", "ret_std", "bias5",
              "max_pullback", "vol_updown", "ret_60d", "up_ratio"):
        x = cols[k]
        edges = np.nanpercentile(x[train], [0, 20, 40, 60, 80, 100])
        line = f"  {k:<18}"
        for i in range(5):
            m = train & (x >= edges[i]) & (x <= edges[i + 1] if i == 4 else x < edges[i + 1])
            if m.sum() < 50:
                line += f"{'-':>14}"
                continue
            line += f"{np.nanmean(cols['fwd_ret_20d'][m]):>+8.1f}%/{np.nanmean(y[m]):>4.0%}"
        print(line)

    if not args.greedy:
        return 0

    # ---- 阶梯规则 ----
    print("\n" + "=" * 104)
    print("阶梯规则（测试集）")
    print("=" * 104)
    print(f"{'规则':<62}{'样本':>8}{'占比':>7}{'20日收益':>10}{'上涨率':>8}{'最大回撤':>10}")
    base_rate = np.nanmean(y[test])
    base_ret = np.nanmean(cols["fwd_ret_20d"][test])
    rules: List[Tuple[str, List[Tuple[str, str, float]]]] = [
        ("A 基础: 上涨占比≥0.62 + 均线多头", [("up_ratio", ">=", 0.62), ("ma_bull", ">=", 1)]),
        ("B +MA20/MA30 双斜率>0", [("up_ratio", ">=", 0.62), ("ma_bull", ">=", 1),
                                   ("ma20_slope", ">", 0), ("ma30_slope", ">", 0)]),
        ("C +MACD 零轴上且红柱占比≥0.5", [("up_ratio", ">=", 0.62), ("ma_bull", ">=", 1),
                                          ("ma20_slope", ">", 0), ("ma30_slope", ">", 0),
                                          ("dif_n", ">", 0), ("dea_n", ">", 0),
                                          ("hist_pos_ratio", ">=", 0.5)]),
        ("D +平滑(日涨幅std≤4) + 回撤≤9%", [("up_ratio", ">=", 0.62), ("ma_bull", ">=", 1),
                                             ("ma20_slope", ">", 0), ("ma30_slope", ">", 0),
                                             ("dif_n", ">", 0), ("dea_n", ">", 0),
                                             ("hist_pos_ratio", ">=", 0.5),
                                             ("ret_std", "<=", 4.0), ("max_pullback", "<=", 0.09)]),
        ("E +乖离不过热(bias5≤8% bias20≤25%)", [("up_ratio", ">=", 0.62), ("ma_bull", ">=", 1),
                                                  ("ma20_slope", ">", 0), ("ma30_slope", ">", 0),
                                                  ("dif_n", ">", 0), ("dea_n", ">", 0),
                                                  ("hist_pos_ratio", ">=", 0.5),
                                                  ("ret_std", "<=", 4.0), ("max_pullback", "<=", 0.09),
                                                  ("bias5", "<=", 8.0), ("bias20", "<=", 25.0)]),
        ("F +涨日量>跌日量", [("up_ratio", ">=", 0.62), ("ma_bull", ">=", 1),
                              ("ma20_slope", ">", 0), ("ma30_slope", ">", 0),
                              ("dif_n", ">", 0), ("dea_n", ">", 0),
                              ("hist_pos_ratio", ">=", 0.5),
                              ("ret_std", "<=", 4.0), ("max_pullback", "<=", 0.09),
                              ("bias5", "<=", 8.0), ("bias20", "<=", 25.0),
                              ("vol_updown", ">=", 1.0)]),
        ("G F + 60日涨幅≤60%(不追高)", [("up_ratio", ">=", 0.62), ("ma_bull", ">=", 1),
                                        ("ma20_slope", ">", 0), ("ma30_slope", ">", 0),
                                        ("dif_n", ">", 0), ("dea_n", ">", 0),
                                        ("hist_pos_ratio", ">=", 0.5),
                                        ("ret_std", "<=", 4.0), ("max_pullback", "<=", 0.09),
                                        ("bias5", "<=", 8.0), ("bias20", "<=", 25.0),
                                        ("vol_updown", ">=", 1.0), ("ret_60d", "<=", 60.0)]),
    ]
    for name, conds in rules:
        m = test.copy()
        for k, op, t in conds:
            x = cols[k]
            if op == ">=":
                m &= x >= t
            elif op == "<=":
                m &= x <= t
            else:
                m &= x > t
        m &= ~np.isnan(cols["fwd_ret_20d"])
        if m.sum() < 20:
            print(f"{name:<62}{int(m.sum()):>8}   样本不足")
            continue
        print(f"{name:<62}{int(m.sum()):>8}{m.sum() / test.sum():>7.1%}"
              f"{np.nanmean(cols['fwd_ret_20d'][m]):>+9.2f}%"
              f"{np.nanmean(y[m]):>8.1%}{np.nanmean(cols['fwd_min_20d'][m]):>+9.2f}%")
    print(f"{'（测试集基准）':<62}{int(test.sum()):>8}{1.0:>7.1%}"
          f"{base_ret:>+9.2f}%{base_rate:>8.1%}"
          f"{np.nanmean(cols['fwd_min_20d'][test]):>+9.2f}%")

    # ---- 分年度稳定性 ----
    print("\n分年度（规则 F）")
    conds = rules[5][1]
    m = np.ones(len(y), bool)
    for k, op, t in conds:
        x = cols[k]
        m &= (x >= t) if op == ">=" else ((x <= t) if op == "<=" else (x > t))
    m &= ~np.isnan(cols["fwd_ret_20d"])
    for yr in sorted({d[:4] for d in dates}):
        ym = np.array([d.startswith(yr) for d in dates])
        if int((ym & test).sum()) < 500:
            continue
        s = m & ym
        if s.sum() < 10:
            continue
        print(f"  {yr}  入选 {int(s.sum()):>5}  20日收益 {np.nanmean(cols['fwd_ret_20d'][s]):>+7.2f}%  "
              f"上涨率 {np.nanmean(y[s]):>6.1%}  基准 {np.nanmean(cols['fwd_ret_20d'][ym & test]):>+7.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
