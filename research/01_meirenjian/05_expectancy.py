"""研究步骤 5：收益结构分解与期望值分析。

回答关键问题：这个形态选出来的票，**实际能不能赚钱**？

用法::

    python research/01_meirenjian/05_expectancy.py --csv research/samples.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Dict, List, Sequence, Tuple

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

FWD = ("fwd_ret", "fwd_gap", "fwd_ret_oc", "fwd_ret_2d", "fwd_ret_3d", "fwd_ret_5d",
       "fwd_max_3d", "fwd_min_3d")
EXTRA = ("gap", "label_next", "label_3d", "decision_date") + FWD

SCORE_FEATURES = (
    "boards_60d", "vol_trend", "gap", "vol_last_ratio", "price",
    "board_vol_ratio", "avg_amp", "ma_spread", "vol_ratio", "hist_n",
    "ret_5d", "close_vs_ma10", "dev_close_min", "dev_close_last",
    "up_days", "max_amp", "vol_vs_pre",
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
    return ({k: np.asarray(v, dtype=float) for k, v in buf.items()},
            np.asarray(labels, dtype=int), np.asarray(dates))


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


def rank_transform(train_vals, vals):
    v = train_vals[~np.isnan(train_vals)]
    if v.size == 0:
        return np.full(len(vals), 0.5)
    v = np.sort(v)
    out = np.clip(np.searchsorted(v, vals, side="left") / len(v), 0.0, 1.0)
    out[np.isnan(vals)] = 0.5
    return out


def stat(cols, sel) -> Dict[str, float]:
    n = int(sel.sum())
    if n == 0:
        return {"n": 0}
    def m(k):
        v = cols[k][sel]
        v = v[~np.isnan(v)]
        return float(np.mean(v)) if v.size else float("nan")
    def med(k):
        v = cols[k][sel]
        v = v[~np.isnan(v)]
        return float(np.median(v)) if v.size else float("nan")
    fr = cols["fwd_ret"][sel]
    fr = fr[~np.isnan(fr)]
    return {
        "n": n,
        "1日": m("fwd_ret"), "1日中位": med("fwd_ret"),
        "次日缺口": m("fwd_gap"),
        "开→收": m("fwd_ret_oc"),
        "2日": m("fwd_ret_2d"), "3日": m("fwd_ret_3d"), "5日": m("fwd_ret_5d"),
        "3日最高": m("fwd_max_3d"), "3日最低": m("fwd_min_3d"),
        "胜率": float((fr > 0).mean()),
        "大亏率": float((fr < -0.05).mean()),
    }


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
    base = float(y[test].mean())
    print(f"样本 {len(y)}  测试集 {int(test.sum())}  测试基准命中率 {base:.2%}\n")

    # ---------- 收益结构分解（测试集） ----------
    print("=" * 96)
    print("① 收益结构：涨停 vs 未涨停（测试集，买入=决策日收盘，卖出=次日收盘）")
    print("=" * 96)
    for lab, nm in ((1, "次日涨停"), (0, "次日未涨停")):
        s = test & (y == lab)
        st = stat(cols, s)
        print(f"  {nm:<10} n={st['n']:>6}  1日均值 {st['1日']:>+7.2%}  "
              f"次日缺口 {st['次日缺口']:>+7.2%}  开→收 {st['开→收']:>+7.2%}")
    st = stat(cols, test)
    print(f"  {'测试全集':<10} n={st['n']:>6}  1日均值 {st['1日']:>+7.2%}  "
          f"胜率 {st['胜率']:>6.1%}  大亏(<-5%)率 {st['大亏率']:.1%}")

    # ---------- 打分模型 ----------
    weights = []
    for k in SCORE_FEATURES:
        a = auc(cols[k][train], y[train])
        weights.append((k, 1 if a >= 0.5 else -1, 2.0 * abs(a - 0.5)))
    tot = sum(w for _, _, w in weights) or 1.0
    weights = [(k, s, w / tot) for k, s, w in weights]
    score = np.zeros(len(y))
    for k, s, w in weights:
        score += w * rank_transform(cols[k][train], s * cols[k])

    print()
    print("=" * 96)
    print("② 打分数值十分位（测试集）")
    print("=" * 96)
    print(f"{'分位':<8}{'样本':>8}{'次日涨停率':>11}{'1日均值':>10}{'开→收':>9}"
          f"{'3日均值':>10}{'3日最高':>10}{'胜率':>8}")
    edges = np.percentile(score[test], np.arange(0, 101, 10))
    for i in range(10):
        lo, hi = edges[i], edges[i + 1]
        s = test & (score >= lo) & (score <= hi if i == 9 else score < hi)
        if s.sum() == 0:
            continue
        st = stat(cols, s)
        print(f"D{i + 1:<7}{st['n']:>8}{y[s].mean():>11.2%}{st['1日']:>10.2%}"
              f"{st['开→收']:>9.2%}{st['3日']:>10.2%}{st['3日最高']:>10.2%}{st['胜率']:>8.1%}")

    # ---------- 规则对比 ----------
    print()
    print("=" * 96)
    print("③ 候选规则对比（测试集；买入=决策日收盘）")
    print("=" * 96)
    rules: Dict[str, List[Tuple[str, str, float]]] = {
        "A 基础(60日涨停≥5, gap 3-6)": [("boards_60d", ">=", 5), ("gap", ">=", 3), ("gap", "<=", 6)],
        "B A+量能撑住(vol_trend≥0.65)": [("boards_60d", ">=", 5), ("gap", ">=", 3), ("gap", "<=", 6),
                                          ("vol_trend", ">=", 0.65)],
        "C B+低价(price≤15)": [("boards_60d", ">=", 5), ("gap", ">=", 3), ("gap", "<=", 6),
                                ("vol_trend", ">=", 0.65), ("price", "<=", 15)],
        "D C+贴板(dev_close_last≥-0.03)": [("boards_60d", ">=", 5), ("gap", ">=", 3), ("gap", "<=", 6),
                                            ("vol_trend", ">=", 0.65), ("price", "<=", 15),
                                            ("dev_close_last", ">=", -0.03)],
        "E 长江通信式(gap 3-8, ±5%窄幅, 不缩量)": [
            ("boards_60d", ">=", 3), ("gap", ">=", 3), ("gap", "<=", 8),
            ("dev_high_max", "<=", 0.05), ("dev_low_min", ">=", -0.05),
            ("vol_ratio", ">=", 0.7), ("vol_ratio", "<=", 1.8)],
        "F E+MACD水上(hist_n>0)": [
            ("boards_60d", ">=", 3), ("gap", ">=", 3), ("gap", "<=", 8),
            ("dev_high_max", "<=", 0.05), ("dev_low_min", ">=", -0.05),
            ("vol_ratio", ">=", 0.7), ("vol_ratio", "<=", 1.8), ("hist_n", ">", 0.0)],
        "G 精筛(60日涨停≥5,gap 3-5,vol_trend≥0.7,close_pos≥0.5,amp≥0.05,price≤20)": [
            ("boards_60d", ">=", 5), ("gap", ">=", 3), ("gap", "<=", 5),
            ("vol_trend", ">=", 0.7), ("close_pos", ">=", 0.5),
            ("avg_amp", ">=", 0.05), ("price", "<=", 20)],
    }
    print(f"{'规则':<52}{'样本':>8}{'涨停率':>9}{'提升':>8}{'1日':>9}{'开→收':>9}{'3日':>9}{'胜率':>8}")
    for name, conds in rules.items():
        mask = test.copy()
        for k, op, t in conds:
            x = cols[k]
            mask &= (x >= t) if op == ">=" else ((x <= t) if op == "<=" else (x > t))
        st = stat(cols, mask)
        if st["n"] == 0:
            continue
        pr = float(y[mask].mean())
        print(f"{name:<52}{st['n']:>8}{pr:>9.2%}{pr / base:>7.2f}x"
              f"{st['1日']:>9.2%}{st['开→收']:>9.2%}{st['3日']:>9.2%}{st['胜率']:>8.1%}")

    # ---------- 最优规则 + 打分排序 ----------
    print()
    print("=" * 96)
    print("④ 最优规则筛池 + 打分每日取 top-k（测试集）")
    print("=" * 96)
    best = rules["G 精筛(60日涨停≥5,gap 3-5,vol_trend≥0.7,close_pos≥0.5,amp≥0.05,price≤20)"]
    pool = test.copy()
    for k, op, t in best:
        x = cols[k]
        pool &= (x >= t) if op == ">=" else ((x <= t) if op == "<=" else (x > t))
    print(f"{'k':>4}{'样本':>8}{'涨停率':>9}{'提升':>8}{'1日':>9}{'开→收':>9}"
          f"{'2日':>9}{'3日':>9}{'3日最高':>11}{'胜率':>8}{'大亏率':>9}")
    for k in (1, 2, 3, 5, 10, 20, 0):
        cand = pool if k == 0 else pool.copy()
        if k:
            picks = []
            for d in np.unique(dates[pool]):
                idx = np.where(pool & (dates == d))[0]
                order = idx[np.argsort(-score[idx], kind="mergesort")]
                picks.extend(order[:k].tolist())
            cand = np.zeros(len(y), dtype=bool)
            cand[picks] = True
        st = stat(cols, cand)
        if st["n"] == 0:
            continue
        pr = float(y[cand].mean())
        label = "全部" if k == 0 else str(k)
        print(f"{label:>4}{st['n']:>8}{pr:>9.2%}{pr / base:>7.2f}x{st['1日']:>9.2%}"
              f"{st['开→收']:>9.2%}{st['2日']:>9.2%}{st['3日']:>9.2%}"
              f"{st['3日最高']:>11.2%}{st['胜率']:>8.1%}{st['大亏率']:>9.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
