"""研究步骤 3：统计正负样本差异，自动挖掘规则并做样本外验证。

用法::

    python research/01_meirenjian/03_analyze.py --csv research/samples.csv
    python research/01_meirenjian/03_analyze.py --csv research/samples.csv --greedy
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

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

# 需要一起读入的附加列
EXTRA_KEYS = ("gap", "fwd_ret", "label_3d", "decision_date", "code", "board_date")

# 方向限制：这些特征越大越好 / 越小越好（None = 由数据决定）
FORCE_DIRECTION: Dict[str, bool] = {
    "dif_n": True,
    "dea_n": True,
    "hist_n": True,
    "close_vs_hh60": True,
    "ret_20d": True,
    "gap": False,
    "close_pos": True,
    "dev_close_min": True,      # 离 0 越近越好（负得越少越好）
    "dev_close_last": True,
    "range_pct": False,         # 振幅越小越好
    "avg_amp": False,
    "max_amp": False,
    "vol_ratio": False,         # 越缩量越好（但见下方区间约束）
    "ma_spread": False,         # 均线越粘合越好
    "ma20_slope": True,
    "dif": True,
    "dea": True,
    "hist": True,
    "ret_10d": True,
    "boards_60d": True,
    "board_vol_ratio": True,
    "dif_up": True,
    "hist_up": True,
    "hist_pos": True,
    "dif_pos": True,
    "dea_pos": True,
    "ma_bull": True,
}

# 强制“区间型”约束（太小的也不要）——用于 vol_ratio 这类
LOWER_BOUNDS: Dict[str, float] = {
    "vol_ratio": 0.40,
    "vol_trend": 0.45,
    "vol_last_ratio": 0.35,
}


def load(path: str, limit: int = 0, since: str = "") -> Tuple[Dict[str, np.ndarray], np.ndarray, List[str]]:
    keys = list(dict.fromkeys(list(FEATURE_KEYS) + list(EXTRA_KEYS)))
    buf: Dict[str, List[float]] = {k: [] for k in keys}
    dates: List[str] = []
    labels: List[int] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                lab = int(float(row.get("label_next") or 0))
            except ValueError:
                continue
            d = row.get("decision_date") or ""
            if since and d < since:
                continue
            labels.append(lab)
            dates.append(d)
            for k in keys:
                v = row.get(k)
                try:
                    buf[k].append(float(v) if v not in (None, "", "None") else float("nan"))
                except ValueError:
                    buf[k].append(float("nan"))
            if limit and len(labels) >= limit:
                break
    cols = {k: np.asarray(v, dtype=float) for k, v in buf.items()}
    return cols, np.asarray(labels, dtype=int), dates


def auc(x: np.ndarray, y: np.ndarray) -> float:
    mask = ~np.isnan(x)
    xx, yy = x[mask], y[mask]
    n1 = int(yy.sum())
    n0 = len(yy) - n1
    if n1 == 0 or n0 == 0:
        return 0.5
    order = np.argsort(xx, kind="mergesort")
    ranks = np.empty(len(xx), dtype=float)
    ranks[order] = np.arange(1, len(xx) + 1, dtype=float)
    return float((ranks[yy == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def q(v: np.ndarray, p: float) -> float:
    v = v[~np.isnan(v)]
    return float(np.percentile(v, p)) if v.size else float("nan")


def evaluate(mask: np.ndarray, y: np.ndarray) -> Dict[str, float]:
    n = int(mask.sum())
    if n == 0:
        return {"n": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0, "lift": 0.0}
    pos = int(y[mask].sum())
    prec = pos / n
    rec = pos / max(int(y.sum()), 1)
    return {
        "n": n,
        "precision": prec,
        "recall": rec,
        "f1": 2 * prec * rec / max(prec + rec, 1e-9),
        "lift": prec / max(float(y.mean()), 1e-9),
    }


# ---------------------------------------------------------------- 贪心找规则


def greedy_rule(
    cols: Dict[str, np.ndarray],
    y: np.ndarray,
    train: np.ndarray,
    max_conditions: int = 6,
    min_selected: int = 200,
) -> List[Tuple[str, float, bool]]:
    """在训练集上贪心拼接条件（最大化 F1）。返回 [(feature, threshold, higher), ...]"""
    conds: List[Tuple[str, float, bool]] = []
    mask = np.ones(len(y), dtype=bool)
    best_f1 = evaluate(mask & train, y)[ "f1"]
    yt = y

    for _ in range(max_conditions):
        best_add = None
        for k in FEATURE_KEYS:
            x = cols[k]
            higher = FORCE_DIRECTION.get(k)
            if higher is None:
                higher = auc(x, yt) >= 0.5
            lo = LOWER_BOUNDS.get(k, -np.inf)
            for pct in range(10, 96, 5):
                t = np.percentile(x[~np.isnan(x)], pct)
                if t < lo:
                    continue
                cand = x >= t if higher else x <= t
                if k in LOWER_BOUNDS:
                    cand = cand & (x >= LOWER_BOUNDS[k])
                m = mask & cand & train
                if int(m.sum()) < min_selected:
                    continue
                f1 = evaluate(m, yt)["f1"]
                if best_add is None or f1 > best_add[0]:
                    best_add = (f1, k, t, higher, cand)
        if best_add is None or best_add[0] <= best_f1 + 1e-4:
            break
        best_f1, k, t, higher, cand = best_add
        mask = mask & cand
        conds.append((k, float(t), higher))
    return conds


def apply_rule(cols: Dict[str, np.ndarray], conds: List[Tuple[str, float, bool]]) -> np.ndarray:
    mask = np.ones(len(next(iter(cols.values()))), dtype=bool)
    for k, t, higher in conds:
        x = cols[k]
        mask &= (x >= t) if higher else (x <= t)
        if k in LOWER_BOUNDS:
            mask &= x >= LOWER_BOUNDS[k]
    return mask


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples.csv"))
    ap.add_argument("--top", type=int, default=26)
    ap.add_argument("--greedy", action="store_true", help="跑贪心规则挖掘 + 样本外验证")
    ap.add_argument("--max-conditions", type=int, default=6)
    ap.add_argument("--since", default="2019-01-01", help="只用该日期之后的样本")
    args = ap.parse_args()

    if np is None:
        print("需要 numpy")
        return 1

    cols, y, dates = load(args.csv, since=args.since)
    base = float(y.mean())
    print(f"样本 {len(y)}   正样本 {int(y.sum())}   基准命中率 {base:.2%}\n")

    rows = []
    for k in FEATURE_KEYS:
        x = cols[k]
        a = auc(x, y)
        rows.append((abs(a - 0.5), k, a))
    rows.sort(reverse=True)

    hdr = (f"{'特征':<17}{'AUC':>7} {'方向':<9}"
           f"{'正样本 p25/p50/p75':>28}{'负样本 p25/p50/p75':>28}")
    print("=" * len(hdr))
    print(hdr)
    print("=" * len(hdr))
    for strength, k, a in rows[: args.top]:
        higher = a >= 0.5
        pos, neg = y == 1, y == 0
        print(
            f"{k:<17}{a:>7.3f} {'越大越好' if higher else '越小越好':<9}"
            f"{q(cols[k][pos], 25):>9.3f}{q(cols[k][pos], 50):>9.3f}{q(cols[k][pos], 75):>9.3f}"
            f"{q(cols[k][neg], 25):>10.3f}{q(cols[k][neg], 50):>9.3f}{q(cols[k][neg], 75):>9.3f}"
        )
    print("=" * len(hdr))

    # ---- 整理天数分布 ----
    print("\n【整理天数 gap 与次日涨停率】")
    gap = cols["gap"]
    for g in range(2, 13):
        m = (gap == g) & ~np.isnan(gap)
        if m.sum() == 0:
            continue
        bar = "█" * int(y[m].mean() * 200)
        print(f"  gap={g:2}  样本{m.sum():>6}   次日涨停率 {y[m].mean():>6.2%}  {bar}")

    # ---- 次日收益 ----
    print("\n【次日收益分布】")
    fwd = cols["fwd_ret"]
    for lab, nm in ((1, "次日涨停"), (0, "次日未涨停")):
        v = fwd[(y == lab) & ~np.isnan(fwd)]
        if v.size:
            print(f"  {nm:<10} n={v.size:>6}  均值{np.mean(v):>+7.2%}  "
                  f"中位{np.median(v):>+7.2%}  胜率{(v > 0).mean():>6.1%}")

    if not args.greedy:
        return 0

    # ---- 贪心规则 + 样本外验证 ----
    rng = np.random.default_rng(42)
    perm = rng.permutation(len(y))
    cut = int(len(y) * 0.7)
    train = np.zeros(len(y), dtype=bool)
    train[perm[:cut]] = True
    test = ~train

    print("\n" + "=" * 78)
    print("贪心规则挖掘（训练集 70% → 测试集 30% 验证）")
    print("=" * 78)
    conds = greedy_rule(cols, y, train, max_conditions=args.max_conditions)
    print("\n最终规则：")
    for k, t, higher in conds:
        lo = f" 且 {k} >= {LOWER_BOUNDS[k]}" if k in LOWER_BOUNDS else ""
        print(f"  {k} {'>=' if higher else '<='} {t:.4f}{lo}")

    mask = apply_rule(cols, conds)
    for nm, sel in (("训练集", mask & train), ("测试集", mask & test), ("全样本", mask)):
        r = evaluate(sel, y)
        print(f"  {nm}: 选中 {r['n']:>6}  精确率 {r['precision']:>6.2%}  "
              f"召回 {r['recall']:>6.2%}  F1 {r['f1']:.3f}  提升 {r['lift']:.2f}x")

    # ---- 每日候选数 ----
    dts = np.asarray(dates)
    sel_dates = np.unique(dts[mask])
    print(f"\n  规则命中覆盖 {len(sel_dates)} 个交易日，"
          f"平均每日候选 {mask.sum() / max(len(sel_dates), 1):.1f} 只")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
