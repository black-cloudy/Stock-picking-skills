"""研究步骤 34：只做 EARLY（早期拐头）信号时，排序 / 价格过滤的真实 alpha。

结论先行（33_feature_alpha）：BREAK（追突破）在 2021-2026 的次日开盘买入口径下
没有超额；EARLY（低位粘合刚拐头）有 +1.4%/20日。本脚本进一步找 EARLY 内部
最好的排序方式。

用法::

    python research/05_lowpos/34_early_alpha.py
"""

from __future__ import annotations

import importlib.util
import os
import pickle
import sys
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

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


def fwd(book: lp.PriceBook, s: Dict[str, Any], h: int = 20) -> Optional[float]:
    p = book.get(s["code"])
    if not p:
        return None
    i = p["idx"].get(s["date"])
    if i is None or i + 1 + h >= len(p["dates"]):
        return None
    entry = p["open"][i + 1]
    prev = p["close"][i]
    if not entry or (prev and entry >= prev * 1.095):
        return None
    return p["close"][i + 1 + h] / entry - 1 - lp.FEE_BUY - lp.FEE_SELL


RANKERS: Dict[str, Optional[Callable[[Dict[str, Any]], float]]] = {
    "随机": None,
    "低位置": lambda s: -s["pos250"],
    "低位置+20日跌": lambda s: -s["pos250"] - 0.8 * s["ret20"],
    "低价格": lambda s: -s["close"],
    "低价格+低位置": lambda s: -s["close"] / 30 - s["pos250"],
    "温和": lambda s: -0.6 * s["pos250"] - 0.3 * s["ret20"] - 0.1 * s["close"] / 30,
    "低MACD": lambda s: -s["hist_slope3"],
    "低量": lambda s: -s["volR"],
}


def main() -> int:
    signals = [s for s in pickle.load(open(SIG, "rb")) if s["kind"] == "EARLY"]
    book = lp.PriceBook(RAW)
    rows = []
    for s in signals:
        r = fwd(book, s)
        if r is not None:
            rows.append({**s, "f20": r})
    print(f"EARLY 样本 {len(rows):,}  mean={np.mean([r['f20'] for r in rows])*100:+.2f}%  "
          f"win={np.mean([r['f20'] > 0 for r in rows])*100:.1f}%")

    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)

    rng = np.random.default_rng(7)
    print(f"\n{'排序':<18}{'价格上限':>8}{'样本':>8}{'均值%':>8}{'胜率%':>7}{'中位%':>8}")
    for price_max in (0, 30, 20, 15, 10):
        pool = rows if not price_max else [r for r in rows if r["close"] <= price_max]
        byd: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in pool:
            byd[r["date"]].append(r)
        for name, fn in RANKERS.items():
            picked = []
            for day, lst in byd.items():
                if fn is None:
                    lst = list(lst)
                    rng.shuffle(lst)
                    picked.extend(lst[:5])
                else:
                    picked.extend(sorted(lst, key=lambda s: -fn(s))[:5])
            a = np.array([r["f20"] for r in picked])
            if not len(a):
                continue
            print(f"{name:<18}{price_max if price_max else '不限':>8}{len(a):>8,}"
                  f"{a.mean()*100:>8.2f}{(a > 0).mean()*100:>7.1f}{np.median(a)*100:>8.2f}")
        print()

    # 按年份看「低价格+低位置」
    print("低价格+低位置 分年度：")
    for y in ("2021", "2022", "2023", "2024", "2025", "2026"):
        picked = []
        for day, lst in by_day.items():
            if day[:4] != y:
                continue
            picked.extend(sorted(lst, key=lambda s: s["close"] / 30 + s["pos250"])[:5])
        a = np.array([r["f20"] for r in picked])
        if len(a):
            print(f"  {y}: n={len(a):>4}  mean={a.mean()*100:+.2f}%  win={(a > 0).mean()*100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
