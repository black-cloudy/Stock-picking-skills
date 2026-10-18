"""研究步骤 32：检查「每天前 5 个信号」的真实次日开盘收益（排序是否有 alpha）。

信号表 .data/lowpos_signals.pkl，价格 raw_bars_deep.db。
对每一天按几种排序取前 5，计算「次日开盘买入 → 持有 h 日收盘卖出」的净收益，
并与「全部信号平均」「随机 5 个」对比。

用法::

    python research/05_lowpos/32_signal_alpha.py
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


def fwd_next_open(book: lp.PriceBook, code: str, day: str, h: int) -> float | None:
    p = book.get(code)
    if not p:
        return None
    ds = p["dates"]
    i = p["idx"].get(day)
    if i is None or i + 1 + h >= len(ds):
        return None
    entry = p["open"][i + 1]
    if not entry:
        return None
    # 次日一字涨停买不到
    prev = p["close"][i]
    if prev and entry >= prev * 1.095:
        return None
    exit_px = p["close"][i + 1 + h]
    if not exit_px:
        return None
    return exit_px / entry - 1 - lp.FEE_BUY - lp.FEE_SELL


def main() -> int:
    signals = pickle.load(open(SIG, "rb"))
    book = lp.PriceBook(RAW)
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in signals:
        s = dict(s)
        s["score_combined"] = lp.sig_score(s, "combined")
        s["score_momentum"] = lp.sig_score(s, "momentum")
        s["score_quality"] = lp.sig_score(s, "quality")
        by_day[s["date"]].append(s)

    flavors = ["score_combined", "score_momentum", "score_quality"]
    rng = np.random.default_rng(7)

    print(f"{'排序':<16}{'h':>3}{'样本':>8}{'均值%':>8}{'胜率%':>7}{'中位%':>8}{'2021%':>8}{'2022%':>8}{'2023%':>8}{'2024%':>8}{'2025%':>8}{'2026%':>8}")
    for flavor in flavors + ["random", "all"]:
        for h in (5, 10, 20):
            rets: List[float] = []
            years: List[str] = []
            for day, lst in by_day.items():
                if flavor == "all":
                    picked = lst
                elif flavor == "random":
                    picked = list(lst)
                    rng.shuffle(picked)
                    picked = picked[:5]
                else:
                    picked = sorted(lst, key=lambda s: -s[flavor])[:5]
                for s in picked:
                    r = fwd_next_open(book, s["code"], day, h)
                    if r is not None:
                        rets.append(r)
                        years.append(day[:4])
            a = np.array(rets)
            yr = np.array(years)
            if not len(a):
                continue
            ystr = "".join(
                f"{a[yr == y].mean()*100:>8.1f}" if (yr == y).any() else f"{'-':>8}"
                for y in ("2021", "2022", "2023", "2024", "2025", "2026"))
            print(f"{flavor:<16}{h:>3}{len(a):>8,}{a.mean()*100:>8.2f}"
                  f"{(a > 0).mean()*100:>7.1f}{np.median(a)*100:>8.2f}{ystr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
