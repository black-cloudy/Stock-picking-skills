"""研究步骤 6：入场/离场方式与持有周期的期望值。

结论导向：这个形态到底该怎么用？

用法::

    python research/01_meirenjian/06_exit.py --csv research/samples.csv
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

from stock_agent.screener.patterns import FEATURE_KEYS  # noqa: E402

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

EXTRA = ("gap", "label_next", "label_3d", "decision_date", "fwd_ret", "fwd_gap",
         "fwd_ret_oc", "fwd_ret_2d", "fwd_ret_3d", "fwd_ret_5d", "fwd_max_3d", "fwd_min_3d")

SCORE_FEATURES = (
    "boards_60d", "vol_trend", "gap", "vol_last_ratio", "price",
    "board_vol_ratio", "avg_amp", "ma_spread", "vol_ratio", "hist_n",
    "ret_5d", "close_vs_ma10", "dev_close_min", "dev_close_last",
    "up_days", "max_amp", "vol_vs_pre",
)


def load(path, since):
    keys = list(dict.fromkeys(list(FEATURE_KEYS) + list(EXTRA)))
    buf = {k: [] for k in keys}
    dates, labels = [], []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            d = row.get("decision_date") or ""
            if since and d < since:
                continue
            try:
                labels.append(int(float(row.get("label_next") or 0)))
            except ValueError:
                continue
            dates.append(d)
            for k in keys:
                v = row.get(k)
                try:
                    buf[k].append(float(v) if v not in (None, "", "None") else float("nan"))
                except ValueError:
                    buf[k].append(float("nan"))
    return ({k: np.asarray(v, float) for k, v in buf.items()},
            np.asarray(labels, int), np.asarray(dates))


def auc(x, y):
    m = ~np.isnan(x)
    xx, yy = x[m], y[m]
    n1, n0 = int(yy.sum()), len(yy) - int(yy.sum())
    if n1 == 0 or n0 == 0:
        return 0.5
    o = np.argsort(xx, kind="mergesort")
    r = np.empty(len(xx), float)
    r[o] = np.arange(1, len(xx) + 1, dtype=float)
    return float((r[yy == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def rank_tr(tv, v):
    tv = tv[~np.isnan(tv)]
    if tv.size == 0:
        return np.full(len(v), 0.5)
    tv = np.sort(tv)
    out = np.clip(np.searchsorted(tv, v, "left") / len(tv), 0, 1)
    out[np.isnan(v)] = 0.5
    return out


def sel_stats(cols, sel, name) -> None:
    n = int(sel.sum())
    if n == 0:
        return
    def mu(k, s=sel):
        v = cols[k][s]
        v = v[~np.isnan(v)]
        return float(np.mean(v)) if v.size else float("nan")
    print(
        f"  {name:<34} n={n:>6}  涨停率 {cols['label_next'][sel].mean():>6.2%}  "
        f"1日 {mu('fwd_ret'):>+7.2%}  3日 {mu('fwd_ret_3d'):>+7.2%}  "
        f"5日 {mu('fwd_ret_5d'):>+7.2%}  开→收 {mu('fwd_ret_oc'):>+7.2%}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples.csv"))
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--split", default="2025-01-01")
    args = ap.parse_args()
    if np is None:
        return 1

    cols, y, dates = load(args.csv, args.since)
    train, test = dates < args.split, dates >= args.split

    weights = []
    for k in SCORE_FEATURES:
        a = auc(cols[k][train], y[train])
        weights.append((k, 1 if a >= 0.5 else -1, 2.0 * abs(a - 0.5)))
    tot = sum(w for _, _, w in weights) or 1.0
    score = np.zeros(len(y))
    for k, s, w in weights:
        score += (w / tot) * rank_tr(cols[k][train], s * cols[k])

    print("=" * 100)
    print("① 抓到二次涨停后的后续走势（测试集，label_next=1 的条件分布）")
    print("=" * 100)
    hit = test & (y == 1)
    for k in ("fwd_ret", "fwd_ret_2d", "fwd_ret_3d", "fwd_ret_5d", "fwd_max_3d", "fwd_min_3d"):
        v = cols[k][hit]
        v = v[~np.isnan(v)]
        print(f"  {k:<14} 均值 {np.mean(v):>+7.2%}  中位 {np.median(v):>+7.2%}  "
              f"胜率 {(v > 0).mean():>6.1%}  >10%占比 {(v > 0.10).mean():>6.1%}")

    print()
    print("=" * 100)
    print("② 不同入场/离场组合（测试集，按打分取每日 top-5，池 = 60日涨停≥5 且 gap 3-6）")
    print("=" * 100)
    pool = test & (cols["boards_60d"] >= 5) & (cols["gap"] >= 3) & (cols["gap"] <= 6)
    picks = []
    for d in np.unique(dates[pool]):
        idx = np.where(pool & (dates == d))[0]
        picks.extend(idx[np.argsort(-score[idx], kind="mergesort")][:5].tolist())
    cand = np.zeros(len(y), bool)
    cand[picks] = True
    print(f"  候选数 {int(cand.sum())}   次日涨停率 {y[cand].mean():.2%}  "
          f"(基准 {y[test].mean():.2%})\n")

    ev = {
        "T+0收盘买 → T+1收盘卖": cols["fwd_ret"][cand],
        "T+1开盘买 → T+1收盘卖": cols["fwd_ret_oc"][cand],
        "T+1开盘买 → T+2收盘卖": cols["fwd_ret_2d"][cand] - cols["fwd_gap"][cand],
        "T+0收盘买 → T+3收盘卖": cols["fwd_ret_3d"][cand],
        "T+0收盘买 → T+5收盘卖": cols["fwd_ret_5d"][cand],
    }
    print(f"  {'入场→离场':<26}{'均值':>10}{'中位':>10}{'胜率':>8}{'最大回撤均值':>14}")
    for name, v in ev.items():
        v = v[~np.isnan(v)]
        if v.size == 0:
            continue
        print(f"  {name:<26}{np.mean(v):>10.2%}{np.median(v):>10.2%}"
              f"{(v > 0).mean():>8.1%}{np.mean(np.minimum(v, 0)):>14.2%}")

    # ---- 目标价 + 止损 ----
    print()
    print("=" * 100)
    print("③ 加入目标价/止损（保守假设：若 3 日内最低价先触及止损则按止损计）")
    print("=" * 100)
    ret3 = cols["fwd_ret_3d"][cand]
    mx = cols["fwd_max_3d"][cand]
    mn = cols["fwd_min_3d"][cand]
    print(f"  {'策略':<34}{'均值':>10}{'中位':>10}{'胜率':>8}{'止盈率':>9}{'止损率':>9}")
    for tp, sl in ((0.05, None), (0.07, None), (0.10, None), (0.05, -0.03),
                   (0.07, -0.04), (0.10, -0.05), (0.15, -0.05)):
        out = ret3.copy()
        hit_sl = np.zeros(len(out), bool)
        hit_tp = np.zeros(len(out), bool)
        if sl is not None:
            hit_sl = mn <= sl
        hit_tp = mx >= tp
        out = np.where(hit_tp & ~hit_sl, tp, out)
        out = np.where(hit_sl, sl if sl is not None else 0, out)
        v = out[~np.isnan(out)]
        lbl = f"止盈{tp:.0%}" + (f" / 止损{sl:.0%}" if sl else " / 无止损")
        print(f"  {lbl:<34}{np.mean(v):>10.2%}{np.median(v):>10.2%}"
              f"{(v > 0).mean():>8.1%}{hit_tp.mean():>9.1%}{hit_sl.mean():>9.1%}")

    # ---- 只在高分位 ----
    print()
    print("=" * 100)
    print("④ 提高打分门槛（池 = 60日涨停≥5 且 gap 3-6）")
    print("=" * 100)
    print(f"  {'门槛':<34}{'候选数':>8}{'涨停率':>9}{'1日':>9}{'开→收':>9}{'3日最高':>10}")
    ths = np.percentile(score[pool], [0, 50, 70, 80, 90, 95, 99])
    for t in ths:
        s = pool & (score >= t)
        if s.sum() < 30:
            continue
        m1 = np.nanmean(cols["fwd_ret"][s])
        moc = np.nanmean(cols["fwd_ret_oc"][s])
        mmx = np.nanmean(cols["fwd_max_3d"][s])
        print(f"  打分 ≥ {t:>7.4f}                  "
              f"{int(s.sum()):>8}{y[s].mean():>9.2%}{m1:>9.2%}{moc:>9.2%}{mmx:>10.2%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
