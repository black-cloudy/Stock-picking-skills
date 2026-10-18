"""研究步骤 17：推导「失败规避规则」并验证。

在步骤 16 的基础上：
  1. 排除换手率缺失（=0）样本，得到干净的换手率结论
  2. 构造「失败规避」附加过滤器
  3. 时间外验证：加规则前后 精确率/召回/每日候选数
  4. 用 2026-09-15 实际失败的 4 只股票做反向检验

用法::

    python research/01_meirenjian/17_avoid_fail.py --csv research/samples.csv
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

from stock_agent.screener.patterns import FEATURE_KEYS, SecondBoardConfig, structure_reasons  # noqa: E402

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

EXTRA = ("decision_date", "fwd_ret", "fwd_min_20d")


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


def q(x, p):
    vals = x[~np.isnan(x)]
    return float(np.percentile(vals, p)) if vals.size else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples.csv"))
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--split", default="2025-01-01")
    args = ap.parse_args()
    if np is None:
        return 1

    cols, y, dates = load(args.csv, args.since)
    n = len(y)
    keys = list(dict.fromkeys(FEATURE_KEYS))
    loose = SecondBoardConfig(min_boards_60d=0, close_band=0.0, min_gap=2, max_gap=12,
                              min_vol_trend=0.0, min_vol_ratio=0.0, max_vol_ratio=999,
                              min_price=0, max_price=99999, min_avg_amp=0, min_hist_n=-9)
    pool = np.zeros(n, bool)
    for idx in range(n):
        feats = {k: (None if np.isnan(cols[k][idx]) else float(cols[k][idx])) for k in keys}
        if not structure_reasons(feats, loose):
            pool[idx] = True
    test = dates >= args.split
    has_to = cols["turnover_last"] > 0

    print(f"池样本 {int(pool.sum())}   其中换手率有效 {int((pool & has_to).sum())} "
          f"({(pool & has_to).sum() / pool.sum():.1%})")
    print(f"池内基准命中率 {y[pool].mean():.2%}   测试集 {y[pool & test].mean():.2%}\n")

    # ---- 干净换手率分档 ----
    print("=" * 96)
    print("换手率分档 → 命中率（已排除缺失值）")
    print("=" * 96)
    for k in ("turnover_board", "turnover_cons", "turnover_last"):
        x = cols[k]
        v = x[pool & has_to]
        edges = np.percentile(v, [0, 20, 40, 60, 80, 100])
        line = f"  {k:<16}"
        for i in range(5):
            lo, hi = edges[i], edges[i + 1]
            m = pool & has_to & (x >= lo) & ((x <= hi) if i == 4 else (x < hi))
            line += f"{y[m].mean():>7.1%}/{q(x[m], 50):>6.2f}" if m.sum() >= 50 else f"{'-':>14}"
        print(line + f"   (基准{y[pool & has_to].mean():.1%})")
    print()

    # ---- 失败规避规则 ----
    RULES = [
        ("A 基准（仅形状）", []),
        ("B +MACD柱未衰减(hist_decline≤0.3)", [("hist_decline", "<=", 0.3)]),
        ("C B+柱未连衰(hist_decline_days≤2)", [("hist_decline", "<=", 0.3), ("hist_decline_days", "<=", 2)]),
        ("D C+首板不放巨量(board_vol_ratio≤2.2)", [
            ("hist_decline", "<=", 0.3), ("hist_decline_days", "<=", 2), ("board_vol_ratio", "<=", 2.2)]),
        ("E D+整理期不缩量(vol_decay≥0.5)", [
            ("hist_decline", "<=", 0.3), ("hist_decline_days", "<=", 2),
            ("board_vol_ratio", "<=", 2.2), ("vol_decay", ">=", 0.5)]),
        ("F E+决策日量能≥0.7(vol_ratio_5d≥0.7)", [
            ("hist_decline", "<=", 0.3), ("hist_decline_days", "<=", 2),
            ("board_vol_ratio", "<=", 2.2), ("vol_decay", ">=", 0.5),
            ("vol_ratio_5d", ">=", 0.7)]),
        ("G F+决策日振幅≥4%(amp_last≥4)", [
            ("hist_decline", "<=", 0.3), ("hist_decline_days", "<=", 2),
            ("board_vol_ratio", "<=", 2.2), ("vol_decay", ">=", 0.5),
            ("vol_ratio_5d", ">=", 0.7), ("amp_last", ">=", 4.0)]),
        ("H G+换手率不过热(turnover_last≤15%)", [
            ("hist_decline", "<=", 0.3), ("hist_decline_days", "<=", 2),
            ("board_vol_ratio", "<=", 2.2), ("vol_decay", ">=", 0.5),
            ("vol_ratio_5d", ">=", 0.7), ("amp_last", ">=", 4.0),
            ("turnover_last", "<=", 15.0)]),
    ]

    def mask_of(conds, base):
        m = base.copy()
        for k, op, t in conds:
            x = cols[k]
            m &= (x >= t) if op == ">=" else (x <= t)
        return m

    print("=" * 104)
    print(f"{'规则':<46}{'测试样本':>9}{'每日':>7}{'命中率':>9}{'提升':>8}{'20日收益':>10}")
    print("=" * 104)
    dtr = pool & test
    days = len(np.unique(dates[dtr]))
    base_rate = y[dtr].mean()
    for name, conds in RULES:
        m = mask_of(conds, dtr)
        if m.sum() < 30:
            print(f"{name:<46}{int(m.sum()):>9}   样本不足")
            continue
        r = y[m].mean()
        ret = np.nanmean(cols["fwd_ret"][m])
        print(f"{name:<46}{int(m.sum()):>9}{m.sum() / days:>7.1f}{r:>9.2%}"
              f"{r / base_rate:>7.2f}x{ret:>+9.2f}%")
    print(f"{'（测试集基准）':<46}{int(dtr.sum()):>9}{dtr.sum() / days:>7.1f}"
          f"{base_rate:>9.2%}{1.0:>7.2f}x{np.nanmean(cols['fwd_ret'][dtr]):>+9.2f}%")
    print("=" * 104)

    # ---- 最严规则 + 打分排序 ----
    print("\n最严规则（G/H）在原始策略池（60日涨停≥5 + 震幅±4% + 整理3~10天）内的效果")
    tight = np.zeros(n, bool)
    cfg2 = SecondBoardConfig(min_boards_60d=5, close_band=0.04, min_gap=3, max_gap=10)
    for idx in range(n):
        feats = {k: (None if np.isnan(cols[k][idx]) else float(cols[k][idx])) for k in keys}
        if not structure_reasons(feats, cfg2):
            tight[idx] = True
    t2 = tight & test
    print(f"  原始策略池: 每日 {t2.sum() / days:.1f} 只  命中率 {y[t2].mean():.2%}")
    for name, conds in RULES[3:]:
        m = mask_of(conds, t2)
        if m.sum() < 10:
            print(f"  {name:<44} 样本不足({int(m.sum())})")
            continue
        print(f"  {name:<44} 每日 {m.sum() / days:.1f} 只  命中率 {y[m].mean():.2%}  "
              f"提升 {y[m].mean() / y[t2].mean():.2f}x")

    # ---- 反向检验：4 只失败股 ----
    print("\n" + "=" * 96)
    print("反向检验：2026-09-15 实际失败的 4 只（决策日 = 2026-09-14）")
    print("=" * 96)
    cases = {
        "002068": {"turnover_last": 10.97, "board_vol_ratio": 1.24, "vol_decay": 0.64,
                   "vol_ratio_5d": 0.90, "amp_last": 10.94, "hist_decline": 0.27,
                   "hist_decline_days": 1, "vol_trend": 0.79},
        "002708": {"turnover_last": 3.88, "board_vol_ratio": 1.10, "vol_decay": 0.56,
                   "vol_ratio_5d": 1.02, "amp_last": 5.84, "hist_decline": 0.35,
                   "hist_decline_days": 2, "vol_trend": 0.61},
        "002194": {"turnover_last": 9.34, "board_vol_ratio": 2.67, "vol_decay": 1.03,
                   "vol_ratio_5d": 0.78, "amp_last": 7.00, "hist_decline": 0.32,
                   "hist_decline_days": 1, "vol_trend": 1.66},
        "002498": {"turnover_last": 4.13, "board_vol_ratio": 1.37, "vol_decay": 0.69,
                   "vol_ratio_5d": 0.98, "amp_last": 5.02, "hist_decline": 0.39,
                   "hist_decline_days": 1, "vol_trend": 0.78},
    }
    names = {"002068": "黑猫股份", "002708": "光洋股份", "002194": "武汉凡谷", "002498": "汉缆股份"}
    g_conds = dict((k, (op, t)) for k, op, t in RULES[6][1])
    for code, f in cases.items():
        bad = []
        if f["hist_decline"] > 0.3: bad.append(f"MACD柱回落{f['hist_decline']:.0%}")
        if f["hist_decline_days"] > 2: bad.append(f"柱连衰{f['hist_decline_days']}天")
        if f["board_vol_ratio"] > 2.2: bad.append(f"首板放量{f['board_vol_ratio']:.2f}倍")
        if f["vol_decay"] < 0.5: bad.append(f"整理期缩量(decay={f['vol_decay']})")
        if f["vol_ratio_5d"] < 0.7: bad.append(f"决策日缩量({f['vol_ratio_5d']})")
        if f["amp_last"] < 4.0: bad.append(f"振幅偏小{f['amp_last']:.1f}%")
        if f["turnover_last"] > 15: bad.append(f"换手率过热{f['turnover_last']}%")
        print(f"  {code} {names[code]:<8} 触发 {len(bad)} 条失败信号: {' / '.join(bad) if bad else '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
