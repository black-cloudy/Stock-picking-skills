"""研究步骤 8：在选定候选池内重新计算特征权重与锚点，生成最优打分模型。

用法::

    python research/01_meirenjian/08_tune.py --csv research/samples.csv
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

from stock_agent.screener.patterns import FEATURE_KEYS  # noqa: E402

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

EXTRA = ("gap", "boards_60d", "decision_date", "label_next", "label_3d",
         "fwd_ret", "fwd_ret_oc", "fwd_ret_3d", "fwd_max_3d", "fwd_min_3d")

# ---------------- 候选池（宽松结构条件，只要满足就不算误伤） ----------------
POOL = (
    ("boards_60d", ">=", 5),
    ("gap", ">=", 3),
    ("gap", "<=", 6),
    ("vol_trend", ">=", 0.55),
    ("vol_ratio", ">=", 0.5),
    ("vol_ratio", "<=", 2.8),
    ("price", ">=", 2.0),
    ("price", "<=", 45.0),
    ("avg_amp", ">=", 0.025),
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
    n1 = int(yy.sum())
    n0 = len(yy) - n1
    if n1 == 0 or n0 == 0:
        return 0.5
    o = np.argsort(xx, kind="mergesort")
    r = np.empty(len(xx), float)
    r[o] = np.arange(1, len(xx) + 1, dtype=float)
    return float((r[yy == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def pq(x, y, lab, p):
    v = x[(y == lab) & ~np.isnan(x)]
    return float(np.percentile(v, p)) if v.size else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples.csv"))
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--split", default="2025-01-01")
    ap.add_argument("--max-features", type=int, default=14)
    ap.add_argument("--min-auc", type=float, default=0.52)
    args = ap.parse_args()
    if np is None:
        return 1

    cols, y, dates = load(args.csv, args.since)
    train = dates < args.split
    test = dates >= args.split

    pool = np.ones(len(y), dtype=bool)
    for k, op, t in POOL:
        x = cols[k]
        pool &= (x >= t) if op == ">=" else (x <= t)
    print(f"候选池条件: {POOL}")
    print(f"  训练池 {int((pool & train).sum())}  测试池 {int((pool & test).sum())}")
    days = len(np.unique(dates[pool & test]))
    print(f"  测试期每日候选 {int((pool & test).sum()) / max(days, 1):.1f} 只"
          f"  命中率 {y[pool & test].mean():.2%}  (基准 {y[test].mean():.2%})\n")

    # ---- 池内重新评估每个特征 ----
    ranked = []
    for k in FEATURE_KEYS:
        a = auc(cols[k][pool & train], y[pool & train])
        ranked.append((abs(a - 0.5), k, a))
    ranked.sort(reverse=True)

    print("池内特征区分度：")
    print(f"  {'特征':<18}{'池内AUC':>9}{'方向':>10}{'负p50':>10}{'正p50':>10}{'正p75':>10}")
    spec = []
    for strength, k, a in ranked[: args.max_features]:
        if strength < args.min_auc - 0.5:
            continue
        inc = a >= 0.5
        neg50 = pq(cols[k], y, 0, 50)
        pos50 = pq(cols[k], y, 1, 50)
        pos75 = pq(cols[k], y, 1, 75)
        lo, hi = (neg50, pos75) if inc else (pos75, neg50)
        if not np.isfinite(lo) or not np.isfinite(hi) or lo == hi:
            continue
        spec.append([k, lo, hi, 2.0 * strength])
        print(f"  {k:<18}{a:>9.3f}{'越大越好' if inc else '越小越好':>10}"
              f"{neg50:>10.3f}{pos50:>10.3f}{pos75:>10.3f}")

    tot = sum(s[3] for s in spec) or 1.0
    for s in spec:
        s[3] = round(s[3] / tot, 3)

    print("\n生成的 SCORE_SPEC：")
    print("SCORE_SPEC: Tuple[Tuple[str, float, float, float], ...] = (")
    for k, lo, hi, w in spec:
        print(f'    ("{k}", {lo:.4g}, {hi:.4g}, {w}),')
    print(")")

    # ---- 应用新打分 ----
    def score_of(sel: np.ndarray) -> np.ndarray:
        out = np.zeros(len(y))
        for k, lo, hi, w in spec:
            x = cols[k]
            frac = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
            frac = np.where(np.isnan(x), 0.5, frac)
            out += w * frac
        return out

    sc = score_of(pool)
    pt = pool & test
    print()
    print("=" * 92)
    print("新打分的效果（测试集池内）")
    print("=" * 92)
    print(f"{'分位':<6}{'样本':>8}{'打分区间':>18}{'命中率':>10}{'提升':>8}"
          f"{'1日均值':>11}{'开→收':>10}{'3日最高':>11}")
    edges = np.percentile(sc[pt], np.arange(0, 101, 10))
    for i in range(10):
        lo, hi = edges[i], edges[i + 1]
        m = pt & (sc >= lo) & ((sc <= hi) if i == 9 else (sc < hi))
        if m.sum() == 0:
            continue
        pr = float(y[m].mean())
        print(f"D{i + 1:<5}{int(m.sum()):>8}{f'{lo:.3f}~{hi:.3f}':>18}{pr:>10.2%}"
              f"{pr / y[test].mean():>7.2f}x{np.nanmean(cols['fwd_ret'][m]):>11.2%}"
              f"{np.nanmean(cols['fwd_ret_oc'][m]):>10.2%}"
              f"{np.nanmean(cols['fwd_max_3d'][m]):>11.2%}")

    print()
    print("=" * 92)
    print("打分阈值 → 命中率 / 每日候选 / 次日开盘买收盘卖收益")
    print("=" * 92)
    print(f"{'打分 ≥':<9}{'候选':>7}{'每日':>7}{'命中率':>10}{'提升':>8}"
          f"{'1日均值':>11}{'开→收':>10}{'开→收胜率':>12}")
    for t in (0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85):
        m = pt & (sc >= t)
        if m.sum() < 30:
            continue
        pr = float(y[m].mean())
        oc = cols["fwd_ret_oc"][m]
        oc = oc[~np.isnan(oc)]
        d = len(np.unique(dates[m]))
        print(f"{t:<9.2f}{int(m.sum()):>7}{m.sum() / max(d, 1):>7.1f}{pr:>10.2%}"
              f"{pr / y[test].mean():>7.2f}x{np.nanmean(cols['fwd_ret'][m]):>11.2%}"
              f"{np.nanmean(oc):>10.2%}{(oc > 0).mean():>12.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
