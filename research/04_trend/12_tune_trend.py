"""研究步骤 12：围绕「长期涨幅低 + 短期强 + 量能健康」做定向规则搜索。

用法::

    python research/04_trend/12_tune_trend.py --csv research/trend_samples.csv
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


def evaluate(cols, sel, name: str, base_ret: float, base_up: float) -> None:
    m = sel & ~np.isnan(cols["fwd_ret_20d"])
    n = int(m.sum())
    if n < 20:
        print(f"  {name:<52}{n:>7}   样本不足")
        return
    ret = np.nanmean(cols["fwd_ret_20d"][m])
    up = np.nanmean((cols["fwd_ret_20d"][m] > 0).astype(float))
    mx = np.nanmean(cols["fwd_max_20d"][m])
    mn = np.nanmean(cols["fwd_min_20d"][m])
    hit10 = np.nanmean((cols["fwd_max_20d"][m] >= 10).astype(float))
    print(f"  {name:<52}{n:>7}{ret:>+9.2f}%{up:>8.1%}{mx:>+9.2f}%{hit10:>8.1%}{mn:>+9.2f}%"
          f"{ret - base_ret:>+9.2f}%")


def apply(cols, conds) -> np.ndarray:
    n = len(cols["price"])
    m = np.ones(n, bool)
    for k, op, t in conds:
        x = cols[k]
        if op == ">=":
            m &= x >= t
        elif op == "<=":
            m &= x <= t
        else:
            m &= x > t
    return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "trend_samples.csv"))
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--split", default="2025-01-01")
    args = ap.parse_args()
    if np is None:
        return 1

    cols, dates = load(args.csv, args.since)
    test = dates >= args.split
    base_ret = np.nanmean(cols["fwd_ret_20d"][test])
    base_up = np.nanmean((cols["fwd_ret_20d"][test] > 0).astype(float))
    print(f"测试集 {int(test.sum())}   基准：20日收益 {base_ret:+.2f}%  上涨率 {base_up:.1%}  "
          f"20日最高 {np.nanmean(cols['fwd_max_20d'][test]):+.2f}%  "
          f"摸到+10%比例 {np.nanmean((cols['fwd_max_20d'][test] >= 10).astype(float)):.1%}  "
          f"最低 {np.nanmean(cols['fwd_min_20d'][test]):+.2f}%\n")

    print("=" * 118)
    print(f"{'规则':<52}{'样本':>7}{'20日收益':>10}{'上涨率':>8}{'20日最高':>10}{'摸+10%':>8}{'最低':>10}{'超额':>10}")
    print("=" * 118)

    SHAPE = [("up_ratio", ">=", 0.62), ("ma_bull", ">=", 1)]
    tests: List[Tuple[str, list]] = [
        ("① 仅形态(上涨≥62%+均线多头)", SHAPE),
        ("② 形态 + 长期没涨(ret_120d≤30%)", SHAPE + [("ret_120d", "<=", 30)]),
        ("③ ② + 短期强(bias5≥2%)", SHAPE + [("ret_120d", "<=", 30), ("bias5", ">=", 2)]),
        ("④ ③ + 量能放大(vol_trend≥1.1)", SHAPE + [("ret_120d", "<=", 30), ("bias5", ">=", 2),
                                                    ("vol_trend", ">=", 1.1)]),
        ("⑤ ④ + 走势平滑(ret_std≤3)", SHAPE + [("ret_120d", "<=", 30), ("bias5", ">=", 2),
                                                ("vol_trend", ">=", 1.1), ("ret_std", "<=", 3.0)]),
        ("⑥ ⑤ + MACD柱放大(hist_slope>0)", SHAPE + [("ret_120d", "<=", 30), ("bias5", ">=", 2),
                                                     ("vol_trend", ">=", 1.1), ("ret_std", "<=", 3.0),
                                                     ("hist_slope", ">", 0)]),
        ("⑦ ⑤ + 涨日量>跌日量", SHAPE + [("ret_120d", "<=", 30), ("bias5", ">=", 2),
                                          ("vol_trend", ">=", 1.1), ("ret_std", "<=", 3.0),
                                          ("vol_updown", ">=", 1.0)]),
        ("⑧ 更严: ret_120d≤15% bias5≥3% 平滑≤2.5", SHAPE + [("ret_120d", "<=", 15), ("bias5", ">=", 3),
                                                             ("vol_trend", ">=", 1.1), ("ret_std", "<=", 2.5)]),
        ("⑨ 接近60日新高(close_vs_hh60≥0.97)", SHAPE + [("ret_120d", "<=", 30), ("bias5", ">=", 2),
                                                         ("vol_trend", ">=", 1.1), ("ret_std", "<=", 3.0),
                                                         ("close_vs_hh60", ">=", 0.97)]),
        ("⑩ 短窗口(win=10) + 长期没涨", [("win", "<=", 10)] + SHAPE + [("ret_120d", "<=", 30)]),
        ("⑪ 长窗口(win=30) + 长期没涨", [("win", ">=", 30)] + SHAPE + [("ret_120d", "<=", 30)]),
        ("⑫ 长期没涨 + 短期强 + 无其他", SHAPE + [("ret_120d", "<=", 20), ("bias10", ">=", 4)]),
    ]
    for name, conds in tests:
        evaluate(cols, apply(cols, conds) & test, name, base_ret, base_up)
    print("=" * 118)

    # ---- 网格：ret_120d × bias5 ----
    print("\n网格：ret_120d 上限 × bias5 下限 （20日收益 / 上涨率）")
    hdr = "  ret_120d≤ |" + "".join(f"{f'bias5≥{b}%':>16}" for b in (0, 2, 4, 6, 8))
    print(hdr)
    for r in (100, 50, 30, 15, 0):
        line = f"  {r:>8}% |"
        for b in (0, 2, 4, 6, 8):
            m = test & (cols["ret_120d"] <= r) & (cols["bias5"] >= b) & apply(cols, SHAPE)
            m &= ~np.isnan(cols["fwd_ret_20d"])
            if m.sum() < 30:
                line += f"{'-':>16}"
                continue
            line += f"{np.nanmean(cols['fwd_ret_20d'][m]):>+9.2f}%/{np.nanmean((cols['fwd_ret_20d'][m]>0).astype(float)):>5.0%}"
        print(line)

    # ---- 分年度 ----
    print("\n分年度（规则 ⑤）")
    conds = tests[4][1]
    m = apply(cols, conds) & ~np.isnan(cols["fwd_ret_20d"])
    for yr in sorted({d[:4] for d in dates}):
        ym = np.array([d.startswith(yr) for d in dates])
        if int((ym & test).sum()) < 500:
            continue
        s = m & ym
        if s.sum() < 10:
            continue
        print(f"  {yr}  入选 {int(s.sum()):>5}  20日收益 {np.nanmean(cols['fwd_ret_20d'][s]):>+7.2f}%  "
              f"上涨率 {np.nanmean((cols['fwd_ret_20d'][s]>0).astype(float)):>6.1%}  "
              f"基准 {np.nanmean(cols['fwd_ret_20d'][ym & test]):>+7.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
