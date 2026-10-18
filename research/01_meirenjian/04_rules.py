"""研究步骤 4：构造打分模型 + 硬性规则，做时间外样本验证。

用法::

    python research/01_meirenjian/04_rules.py --csv research/samples.csv
    python research/01_meirenjian/04_rules.py --csv research/samples.csv --split 2025-01-01
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Any, Dict, List, Sequence, Tuple

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

EXTRA = ("gap", "fwd_ret", "fwd_ret_2d", "fwd_ret_3d", "fwd_ret_5d",
         "fwd_gap", "fwd_ret_oc", "fwd_max_3d", "fwd_min_3d",
         "label_3d", "decision_date")

# 参与打分的特征（由 AUC 决定权重；方向 = 数据方向，+1 越大越好）
SCORE_FEATURES = (
    "boards_60d", "vol_trend", "gap", "vol_last_ratio", "price",
    "board_vol_ratio", "avg_amp", "ma_spread", "vol_ratio", "hist_n",
    "ret_5d", "close_vs_ma10", "dev_close_min", "dev_close_last",
    "up_days", "max_amp", "vol_vs_pre",
)


def load(path: str, since: str = "2019-01-01"):
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
    return ({k: np.asarray(v, dtype=float) for k, v in buf.items()},
            np.asarray(labels, dtype=int), np.asarray(dates))


def auc(x: np.ndarray, y: np.ndarray) -> float:
    m = ~np.isnan(x)
    xx, yy = x[m], y[m]
    n1 = int(yy.sum())
    n0 = len(yy) - n1
    if n1 == 0 or n0 == 0:
        return 0.5
    order = np.argsort(xx, kind="mergesort")
    ranks = np.empty(len(xx), float)
    ranks[order] = np.arange(1, len(xx) + 1, dtype=float)
    return float((ranks[yy == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def metrics(y: np.ndarray, sel: np.ndarray, fwd: np.ndarray) -> Dict[str, float]:
    n = int(sel.sum())
    if n == 0:
        return {"n": 0, "prec": 0.0, "rec": 0.0, "lift": 0.0, "ret": 0.0, "win": 0.0}
    pos = int(y[sel].sum())
    prec = pos / n
    r = fwd[sel]
    r = r[~np.isnan(r)]
    return {
        "n": n,
        "prec": prec,
        "rec": pos / max(int(y.sum()), 1),
        "lift": prec / max(float(y.mean()), 1e-9),
        "ret": float(np.mean(r)) if r.size else 0.0,
        "win": float((r > 0).mean()) if r.size else 0.0,
    }


def build_weights(cols, y, train) -> List[Tuple[str, int, float]]:
    out = []
    for k in SCORE_FEATURES:
        a = auc(cols[k][train], y[train])
        sign = 1 if a >= 0.5 else -1
        out.append((k, sign, 2.0 * abs(a - 0.5)))
    total = sum(w for _, _, w in out) or 1.0
    return [(k, s, w / total) for k, s, w in out]


def rank_transform(train_vals: np.ndarray, vals: np.ndarray) -> np.ndarray:
    """把值映射为它在训练分布中的分位（0~1）。"""
    v = train_vals[~np.isnan(train_vals)]
    if v.size == 0:
        return np.full(len(vals), 0.5)
    v = np.sort(v)
    out = np.searchsorted(v, vals, side="left") / len(v)
    out = np.clip(out, 0.0, 1.0)
    out[np.isnan(vals)] = 0.5
    return out


def apply_hard(cols, conds) -> np.ndarray:
    n = len(next(iter(cols.values())))
    mask = np.ones(n, dtype=bool)
    for k, op, t in conds:
        x = cols[k]
        mask &= (x >= t) if op == ">=" else (x <= t)
    return mask


def daily_topk(dates: np.ndarray, score: np.ndarray, y: np.ndarray,
               fwd: np.ndarray, k: int, cand: np.ndarray) -> Dict[str, float]:
    """每天的候选池 cand 里按 score 取前 k 只，汇总评估（cand 已是全样本长度）。"""
    picks: List[int] = []
    for d in np.unique(dates[cand]):
        idx = np.where(cand & (dates == d))[0]
        if idx.size == 0:
            continue
        order = idx[np.argsort(-score[idx], kind="mergesort")]
        picks.extend(order[:k].tolist())
    sel = np.zeros(len(y), dtype=bool)
    sel[picks] = True
    return metrics(y, sel, fwd)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples.csv"))
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--split", default="2025-01-01", help="该日期之后作为测试集")
    ap.add_argument("--k", type=int, default=10, help="每日取前 k 只")
    args = ap.parse_args()

    if np is None:
        print("需要 numpy")
        return 1

    cols, y, dates = load(args.csv, args.since)
    fwd = cols["fwd_ret"]
    train = dates < args.split
    test = ~train
    print(f"样本 {len(y)}   训练 {train.sum()} (至 {args.split})   测试 {test.sum()}")
    print(f"基准命中率: 训练 {y[train].mean():.2%}   测试 {y[test].mean():.2%}\n")

    # ---------------- 打分权重 ----------------
    weights = build_weights(cols, y, train)
    print("打分权重（训练集 AUC 推导）：")
    for k, s, w in sorted(weights, key=lambda r: -r[2]):
        print(f"   {k:<17} 方向 {'越大越好' if s > 0 else '越小越好'}   权重 {w:6.3f}")
    print(f"   权重合计 {sum(w for _, _, w in weights):.3f}\n")

    score = np.zeros(len(y))
    for k, s, w in weights:
        score += w * rank_transform(cols[k][train], s * cols[k])

    # ---------------- 打分模型评估 ----------------
    print("=" * 92)
    print("① 打分模型：每日在候选池中取 top-k")
    print("=" * 92)
    print(f"{'候选池':<26}{'k':>4}{'选中':>8}{'精确率':>9}{'提升':>8}{'次日均收益':>12}{'胜率':>8}")
    pools = {
        "全部样本": np.ones(len(y), dtype=bool),
        "boards_60d>=3": apply_hard(cols, [("boards_60d", ">=", 3)]),
        "boards_60d>=5": apply_hard(cols, [("boards_60d", ">=", 5)]),
        "boards_60d>=5 & gap<=6": apply_hard(cols, [("boards_60d", ">=", 5), ("gap", "<=", 6)]),
    }
    for name, pool in pools.items():
        for k in (5, 10, 20):
            r = daily_topk(dates, score, y, fwd, k, pool & test)
            print(f"{name:<26}{k:>4}{r['n']:>8}{r['prec']:>9.2%}{r['lift']:>8.2f}x"
                  f"{r['ret']:>12.2%}{r['win']:>8.1%}")

    # ---------------- 硬性规则 ----------------
    print()
    print("=" * 92)
    print("② 硬性规则（全测试集直接筛选，不做 top-k）")
    print("=" * 92)
    print(f"{'规则':<58}{'选中':>8}{'精确率':>9}{'提升':>8}{'次日均收益':>12}{'胜率':>8}")
    rules = {
        "R1 60日涨停>=5, gap<=4": [("boards_60d", ">=", 5), ("gap", "<=", 4)],
        "R2 R1 + vol_trend>=0.65": [("boards_60d", ">=", 5), ("gap", "<=", 4), ("vol_trend", ">=", 0.65)],
        "R3 R2 + price<=20": [("boards_60d", ">=", 5), ("gap", "<=", 4), ("vol_trend", ">=", 0.65), ("price", "<=", 20)],
        "R4 60日涨停>=7, gap<=5, vol_last>=0.5": [("boards_60d", ">=", 7), ("gap", "<=", 5), ("vol_last_ratio", ">=", 0.5)],
        "R5 60日涨停>=3, gap<=3, vol_trend>=0.7": [("boards_60d", ">=", 3), ("gap", "<=", 3), ("vol_trend", ">=", 0.7)],
        "R6 R2 + dev_close_last>=-0.04": [("boards_60d", ">=", 5), ("gap", "<=", 4), ("vol_trend", ">=", 0.65), ("dev_close_last", ">=", -0.04)],
        "R7 R2 + hist_n>0.005": [("boards_60d", ">=", 5), ("gap", "<=", 4), ("vol_trend", ">=", 0.65), ("hist_n", ">=", 0.005)],
        "R8 60日涨停>=5,gap<=5,vol_trend>=0.75,amp>=0.05": [
            ("boards_60d", ">=", 5), ("gap", "<=", 5), ("vol_trend", ">=", 0.75), ("avg_amp", ">=", 0.05)],
    }
    for name, conds in rules.items():
        sel = apply_hard(cols, conds)
        r = metrics(y, sel & test, fwd)
        print(f"{name:<58}{r['n']:>8}{r['prec']:>9.2%}{r['lift']:>8.2f}x"
              f"{r['ret']:>12.2%}{r['win']:>8.1%}")

    # ---------------- 硬规则 + 打分排序 ----------------
    print()
    print("=" * 92)
    print("③ 硬性规则筛池 + 打分排序取 top-k（最贴近实盘用法）")
    print("=" * 92)
    print(f"{'规则池':<44}{'k':>4}{'选中':>8}{'精确率':>9}{'提升':>8}{'次日均收益':>12}{'胜率':>8}")
    for name, conds in list(rules.items())[:5]:
        pool = apply_hard(cols, conds) & test
        for k in (5, 10):
            r = daily_topk(dates, score, y, fwd, k, pool)
            print(f"{name:<44}{k:>4}{r['n']:>8}{r['prec']:>9.2%}{r['lift']:>8.2f}x"
                  f"{r['ret']:>12.2%}{r['win']:>8.1%}")

    # ---------------- 分年度稳定性 ----------------
    print()
    print("=" * 92)
    print("④ 稳定性：规则 R2 按年份")
    print("=" * 92)
    conds = rules["R2 R1 + vol_trend>=0.65"]
    sel = apply_hard(cols, conds)
    for yr in sorted({d[:4] for d in dates}):
        m = np.array([d.startswith(yr) for d in dates])
        if m.sum() < 500:
            continue
        r = metrics(y, sel & m, fwd)
        base = y[m].mean()
        print(f"  {yr}  基准 {base:>6.2%}   规则命中 {r['n']:>6}   精确率 {r['prec']:>6.2%}"
              f"   提升 {r['lift']:>5.2f}x   次日均收益 {r['ret']:>+6.2%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
