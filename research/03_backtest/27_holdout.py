"""研究步骤 27：美人肩「持有到被剔除」的最终兑现率。

用户口径：今天选出来的票，从选中那一刻一直持有，直到它被"剔除"——
  * 涨停了（命中，二次涨停兑现）→ 剔除
  * 往下跌破横盘下沿（收盘 < 锚点价 × (1-close_band)）→ 剔除（失败）
  * 往上突破横盘上沿（收盘 > 锚点价 × (1+close_band)，但没涨停）→ 剔除（冲高未封）
  * 横盘超过 max_gap 天还不动 → 剔除（超时）

统计：被选中的票里，最终有多少兑现了涨停（而不是 3 日内这种短窗口）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.patterns import SecondBoardConfig, StockContext  # noqa: E402
from stock_agent.screener.scanner import default_store_path  # noqa: E402
from stock_agent.screener.store import ScreenerStore  # noqa: E402

ROOT = _p
META = os.path.join(ROOT, ".data", "meta.json")


def _mean(xs):
    xs = [x for x in xs if x]
    return sum(xs) / len(xs) if xs else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-start", default="2021-09-17")
    ap.add_argument("--window-end", default="2026-09-17")
    args = ap.parse_args()

    store = ScreenerStore(default_store_path())
    names: dict = {}
    shares: dict = {}
    if os.path.exists(META):
        meta = json.load(open(META, encoding="utf-8"))
        for c, v in meta.items():
            names[c] = v.get("name", "")
            shares[c] = v.get("float_shares", 0.0)

    with store._connect() as conn:
        conn.row_factory = None
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_bars WHERE adjust='bfq' ORDER BY code")]
    codes = [c for c in codes if c.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))]

    cfg = SecondBoardConfig()

    total_events = 0          # 所有「涨停/准涨停后横盘」事件
    selected = 0              # 被策略选中（今天这种"列出来"）
    outcome_cnt: Counter = Counter()          # 选中事件的最终结果
    hit_gaps: list = []       # 命中时距锚点的天数
    hit_from_sel: list = []   # 命中时距选中日的天数

    for code in codes:
        bars = store.get_bars(code, "bfq")
        if not bars:
            continue
        ctx = StockContext(bars, code, names.get(code, ""), shares.get(code, 0.0))
        for i in range(1, ctx.n):
            if not ctx.is_anchor(i, cfg.anchor_min_gain, cfg.anchor_max_gain):
                continue
            dt = str(bars[i].get("date") or "")
            if dt < args.window_start or dt > args.window_end:
                continue
            if i + cfg.max_gap + 1 > ctx.n:
                continue  # 需要能观察到完整窗口
            L = ctx.closes[i]
            if not L:
                continue
            total_events += 1

            is_sel = False
            sel_gap = None
            outcome = None
            exit_gap = None
            for d in range(i + 1, i + cfg.max_gap + 1):
                gap = d - i
                c = ctx.closes[d]
                if c is None:
                    break
                # 1) 涨停 → 命中
                if ctx.lu[d]:
                    outcome = "涨停"; exit_gap = gap; break
                # 2) 跌破下沿
                if c < L * (1 - cfg.close_band):
                    outcome = "跌破下沿"; exit_gap = gap; break
                # 3) 向上突破（冲高但没封板）
                if c > L * (1 + cfg.close_band):
                    outcome = "向上突破"; exit_gap = gap; break
                # 4) 仍在横盘：看这一天是否够格被选中（gap 3~15）
                if not is_sel and gap >= cfg.min_gap:
                    hi = max(ctx.highs[i + 1:d + 1])
                    lo = min(ctx.lows[i + 1:d + 1])
                    if (hi - lo) / L <= cfg.max_range_pct:
                        head2 = ctx.vols[i + 1:i + 3]
                        tail2 = ctx.vols[max(i + 1, d - 1):d + 1]
                        vt = _mean(tail2) / _mean(head2) if _mean(head2) else 0.0
                        if vt >= cfg.min_vol_trend:
                            is_sel = True
                            sel_gap = gap
                # 5) 超时
                if d == i + cfg.max_gap:
                    outcome = "超时"; exit_gap = gap

            if outcome is None:
                continue
            if is_sel:
                selected += 1
                outcome_cnt[outcome] += 1
                if outcome == "涨停":
                    hit_gaps.append(exit_gap)
                    hit_from_sel.append(exit_gap - (sel_gap or exit_gap))

    hit = outcome_cnt.get("涨停", 0)
    print(f"窗口 {args.window_start} ~ {args.window_end}  沪深主板 {len(codes)} 只")
    print(f"锚点事件（涨停/准涨停后横盘）: {total_events:,}")
    print(f"其中被策略选中（今天列表这种）: {selected:,}")
    print()
    print("=" * 60)
    print("【选中后持有到被剔除，最终结果】")
    print("=" * 60)
    for k, label in (("涨停", "✅ 二次涨停（命中）"),
                     ("向上突破", "⬆ 冲高突破 +7% 但未封板"),
                     ("跌破下沿", "⬇ 跌破横盘下沿 -7%"),
                     ("超时", "⏱ 横盘超 15 天没动静")):
        n = outcome_cnt.get(k, 0)
        print(f"  {label:<26} {n:>7,}  ({n/max(selected,1):>6.1%})")
    print()
    print(f"最终涨停命中率 = {hit}/{selected} = {hit/max(selected,1):.1%}")
    if hit_gaps:
        print(f"涨停发生时：距锚点平均 {sum(hit_gaps)/len(hit_gaps):.1f} 天，"
              f"距选中日平均 {sum(hit_from_sel)/len(hit_from_sel):.1f} 天")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
