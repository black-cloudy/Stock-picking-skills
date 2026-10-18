"""研究步骤 24：美人肩策略历史回测。

假设回到 2021-09-17，用之前的数据，统计策略能预测到多少波涨停、成功率多少、
漏掉多少。

回测口径（大道至简版）：
  * 锚点日涨幅 ≥ 4%（涨停也算）
  * 横盘 3~15 天，收盘震幅 ±5%，总振幅 ≤15%
  * 还没启动（横盘期内未涨停）
  * 沪深主板，排除 ST

指标定义：
  * **信号** = 某天策略会输出的一个候选（决策点）
  * **命中** = 信号之后 3 个交易日内出现涨停
  * **精度** = 命中数 / 信号数
  * **涨停波** = 一次「锚点→横盘→涨停」的完整事件（label_3d=1 去重）
  * **识别到** = 涨停波在其横盘期被至少一次信号标记过
  * **召回** = 识别到的涨停波 / 总涨停波；**漏掉率** = 1 - 召回

用法::

    python research/03_backtest/24_backtest.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.patterns import (  # noqa: E402
    FEATURE_KEYS,
    MineConfig,
    SecondBoardConfig,
    mine_stock,
    structure_reasons,
)
from stock_agent.screener.scanner import default_store_path  # noqa: E402
from stock_agent.screener.store import ScreenerStore  # noqa: E402

ROOT = _p
META = os.path.join(ROOT, ".data", "meta.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-start", default="2016-09-17")
    ap.add_argument("--window-end", default="2021-09-17")
    ap.add_argument("--market", default="main", choices=["main", "sz", "sh", "all"])
    ap.add_argument("--anchor-min-gain", type=float, default=4.0)
    args = ap.parse_args()

    store = ScreenerStore(default_store_path())
    names: Dict[str, str] = {}
    shares: Dict[str, float] = {}
    if os.path.exists(META):
        meta = json.load(open(META, encoding="utf-8"))
        for c, v in meta.items():
            names[c] = v.get("name", "")
            shares[c] = v.get("float_shares", 0.0)

    def keep(c: str) -> bool:
        if c.startswith(("4", "8", "920")):
            return False
        if args.market == "main":
            return c.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))
        if args.market == "sz":
            return c.startswith(("000", "001", "002", "003"))
        if args.market == "sh":
            return c.startswith(("600", "601", "603", "605"))
        return True

    with store._connect() as conn:
        conn.row_factory = None
        codes = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT code FROM daily_bars WHERE adjust='bfq' ORDER BY code"
            )
        ]
    codes = [c for c in codes if keep(c)]
    print(f"股票池（{args.market}）: {len(codes)} 只")

    cfg = SecondBoardConfig()
    cfg.anchor_min_gain = args.anchor_min_gain
    mine_cfg = MineConfig(
        anchor_min_gain=args.anchor_min_gain,
        anchor_max_gain=cfg.anchor_max_gain,
        min_gap=cfg.min_gap,
        max_gap=cfg.max_gap,
        max_abs_dev=0.10,
    )

    # 只取窗口内的 bar（但需要窗口前至少 30 根做 MA/MACD 预热）
    n_signals = 0
    n_hits = 0
    events: set = set()            # (code, board_date) 所有锚点事件
    lu_events: set = set()         # 涨停波事件
    flagged_events: set = set()    # 被信号标记过的事件
    per_day: Counter = Counter()   # 每日信号数
    per_year: Counter = Counter()  # 每年信号数
    hit_per_year: Counter = Counter()
    by_anchor: Dict[str, list] = {"limitup": [0, 0], "mild": [0, 0]}  # [信号, 命中]

    n_stocks = 0
    for code in codes:
        bars = store.get_bars(code, "bfq")
        # 只保留窗口内 + 窗口前预热部分
        bars = [b for b in bars if b.get("date", "") >= args.window_start[:4]]
        bars = [b for b in bars if b.get("date", "") <= args.window_end]
        if len(bars) < 80:
            continue
        n_stocks += 1
        recs = mine_stock(bars, code, names.get(code, ""), mine_cfg,
                          float_shares=shares.get(code, 0.0))
        for r in recs:
            d = r.get("decision_date") or ""
            if d < args.window_start or d > args.window_end:
                continue
            ev = (r["code"], r["board_date"])
            events.add(ev)
            if r["label_3d"]:
                lu_events.add(ev)
            # 应用当前简化配置
            feats = {k: r.get(k) for k in FEATURE_KEYS if k in r}
            if structure_reasons(feats, cfg):
                continue
            # 这是一个信号
            n_signals += 1
            per_day[d] += 1
            per_year[d[:4]] += 1
            flagged_events.add(ev)
            is_lu = bool(feats.get("anchor_is_lu"))
            key = "limitup" if is_lu else "mild"
            by_anchor[key][0] += 1
            if r["label_3d"]:
                n_hits += 1
                hit_per_year[d[:4]] += 1
                by_anchor[key][1] += 1

    print(f"有数据的股票: {n_stocks} 只\n")

    total_days = len(per_day)
    # 涨停波中被识别到的
    recognized = flagged_events & lu_events
    missed = lu_events - flagged_events

    print("=" * 70)
    print("【回测结果】窗口 %s ~ %s" % (args.window_start, args.window_end))
    print("=" * 70)
    print(f"锚点事件总数        : {len(events):>8,}")
    print(f"涨停波（锚点→横盘→涨停）: {len(lu_events):>8,}")
    print(f"  其中被策略识别到  : {len(recognized):>8,}  (召回 {len(recognized) / max(len(lu_events), 1):.1%})")
    print(f"  漏掉的涨停波      : {len(missed):>8,}  (漏掉率 {len(missed) / max(len(lu_events), 1):.1%})")
    print()
    print(f"策略信号总数        : {n_signals:>8,}")
    print(f"信号命中（3日内涨停）: {n_hits:>8,}")
    print(f"信号精度            : {n_hits / max(n_signals, 1):>8.1%}")
    print(f"平均每日信号        : {n_signals / max(total_days, 1):>8.1f} 只")
    print(f"平均每日命中        : {n_hits / max(total_days, 1):>8.2f} 只")
    print()
    print(f"被标记事件总数      : {len(flagged_events):>8,}")
    print(f"事件级精度(被标记事件最终涨停率) : {len(recognized) / max(len(flagged_events), 1):>8.1%}")
    # 基准
    base_events = len(lu_events) / max(len(events), 1)
    print(f"基准（任意锚点事件最终涨停率）: {base_events:>8.1%}")
    print(f"策略相对基准提升              : {n_hits / max(n_signals, 1) / max(base_events, 1e-9):>7.2f}x")

    print()
    print("【分锚点类型】")
    for key, label in (("limitup", "涨停锚点(方式1)"), ("mild", "温和阳线锚点(方式2)")):
        sig, hit = by_anchor[key]
        print(f"  {label}: 信号 {sig:>7,}  命中 {hit:>7,}  精度 {hit / max(sig, 1):>7.1%}")

    print()
    print("【分年份】")
    print(f"  {'年份':<6}{'信号':>9}{'命中':>9}{'精度':>9}")
    for y in sorted(per_year):
        s = per_year[y]
        h = hit_per_year[y]
        print(f"  {y:<6}{s:>9,}{h:>9,}{h / max(s, 1):>9.1%}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
