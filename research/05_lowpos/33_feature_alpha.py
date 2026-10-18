"""研究步骤 33：信号特征 → 次日开盘买入后的真实收益（分层分析）。

目的：找出哪些特征/排序方向真正有正向 alpha，供组合回测使用。
输出每个特征的 5 分位平均 20 日净收益（次日开盘买入 → 20 日后收盘）。

用法::

    python research/05_lowpos/33_feature_alpha.py
"""

from __future__ import annotations

import importlib.util
import os
import pickle
import sys
from collections import defaultdict
from typing import Any, Dict, List

import numpy as np

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

spec = importlib.util.spec_from_file_location(
    "lp_port", os.path.join(_p, "research", "05_lowpos", "31_lowpos_portfolio.py"))
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)  # type: ignore[union-attr]

SIG = os.path.join(lp.ROOT, ".data", "lowpos_signals.pkl")
RAW = os.path.join(lp.ROOT, ".data", "raw_bars_deep.db")


def main() -> int:
    signals = pickle.load(open(SIG, "rb"))
    book = lp.PriceBook(RAW)
    rows: List[Dict[str, Any]] = []
    for s in signals:
        r = None
        p = book.get(s["code"])
        if p:
            i = p["idx"].get(s["date"])
            if i is not None and i + 21 < len(p["dates"]):
                entry = p["open"][i + 1]
                prev = p["close"][i]
                if entry and not (prev and entry >= prev * 1.095):
                    r = p["close"][i + 21] / entry - 1 - lp.FEE_BUY - lp.FEE_SELL
        if r is not None:
            rows.append({**s, "f20": r})
    print(f"样本 {len(rows):,}")

    years = np.array([r["date"][:4] for r in rows])
    kinds = np.array([r["kind"] for r in rows])
    ret = np.array([r["f20"] for r in rows])

    print("\n按模式 / 年份（20日净收益均值）：")
    for kind in ("EARLY", "BREAK"):
        m = kinds == kind
        print(f"  {kind}: n={m.sum():,}  mean={ret[m].mean()*100:+.2f}%  "
              f"win={(ret[m] > 0).mean()*100:.1f}%  " +
              "  ".join(f"{y}:{ret[m & (years == y)].mean()*100:+.1f}%"
                        for y in ("2021", "2022", "2023", "2024", "2025", "2026")))

    feats = ["spread10", "spread20", "pos250", "dd250", "dd120", "bias20",
             "chg1", "chg3", "volR", "hist", "hist_slope3", "ret20", "close"]
    print("\n特征 5 分位平均 20 日净收益（%）：")
    print(f"{'特征':<14}" + "".join(f"{'Q'+str(i):>9}" for i in range(1, 6)) + f"{'多空Q5-Q1':>11}")
    for f in feats:
        x = np.array([r[f] for r in rows], dtype=float)
        ok = np.isfinite(x)
        qs = np.quantile(x[ok], [0.2, 0.4, 0.6, 0.8])
        bins = np.digitize(x, qs)
        means = []
        for b in range(5):
            m = ok & (bins == b)
            means.append(ret[m].mean() * 100 if m.any() else np.nan)
        print(f"{f:<14}" + "".join(f"{v:>9.2f}" for v in means) +
              f"{means[-1]-means[0]:>11.2f}")

    # 排序特征组合的简单测试
    print("\n几种候选排序的 top5 / 全部：")
    tests = {
        "随机": None,
        "低位置": lambda s: -s["pos250"],
        "低位置+20日跌": lambda s: -s["pos250"] - s["ret20"],
        "最松粘合": lambda s: s["spread20"],
        "最紧粘合": lambda s: -s["spread20"],
        "放量": lambda s: s["volR"],
        "温和阳线": lambda s: -abs(s["chg1"] - 0.03),
        "低PE?价格低": lambda s: -s["close"],
        "突破+低位置": lambda s: -s["pos250"] + 0.5 * s["chg1"],
    }
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)
    for name, fn in tests.items():
        picked = []
        for day, lst in by_day.items():
            if fn is None:
                picked.extend(lst[:5])
            else:
                picked.extend(sorted(lst, key=lambda s: -fn(s))[:5])
        a = np.array([r["f20"] for r in picked])
        print(f"  {name:<16} n={len(a):>7,}  mean={a.mean()*100:+.2f}%  "
              f"win={(a > 0).mean()*100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
