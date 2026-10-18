"""研究步骤 1：抓取全市场 800 根前复权日线，建立深度历史库。

用法::

    python research/01_meirenjian/01_fetch_deep.py --concurrency 24 --bars 800
    python research/01_meirenjian/01_fetch_deep.py --limit 300          # 小样本试跑
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent import MarketService  # noqa: E402
from stock_agent.screener.scanner import default_store_path  # noqa: E402
from stock_agent.screener.store import ScreenerStore  # noqa: E402

ROOT = _p
NAMES_PATH = os.path.join(ROOT, ".data", "names.json")


def is_bj(code: str) -> bool:
    """北交所：4xxxxx / 8xxxxx / 920xxx。"""
    return code.startswith(("4", "8", "920"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=24)
    ap.add_argument("--bars", type=int, default=800)
    ap.add_argument("--adjust", default="qfq")
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 只（调试用）")
    ap.add_argument("--include-bj", action="store_true", help="包含北交所（腾讯源无数据）")
    ap.add_argument("--max-fail-rate", type=float, default=0.5,
                    help="失败率超过该值立即中止（防限流时空跑）")
    args = ap.parse_args()

    store = ScreenerStore(default_store_path())

    async with MarketService() as svc:
        svc.set_concurrency(args.concurrency)
        print("拉取全市场股票池 ...")
        universe = await svc.all_stocks("all")
        universe.sort(key=lambda it: it["code"])
        print(f"  共 {len(universe)} 只")

        os.makedirs(os.path.dirname(NAMES_PATH), exist_ok=True)
        with open(NAMES_PATH, "w", encoding="utf-8") as fh:
            json.dump({it["code"]: (it.get("name") or "") for it in universe},
                      fh, ensure_ascii=False)

        codes = [it["code"] for it in universe]
        if not args.include_bj:
            codes = [c for c in codes if not is_bj(c)]
        if args.limit:
            codes = codes[: args.limit]

        state = store.fetch_state(codes, args.adjust)
        todo = [c for c in codes if state.get(c, (0, 0))[1] < args.bars]
        print(f"需要抓取: {len(todo)} 只（已有 {len(codes) - len(todo)} 只）")
        if not todo:
            print("已完成。")
            return 0

        print("探测可用数据源 ...")
        order = await svc.auto_order("kline")
        print(f"  数据源顺序: {order}")

        done = fail = 0
        t0 = time.time()
        lock = asyncio.Lock()
        aborted = False

        async def work(code: str) -> None:
            nonlocal done, fail, aborted
            bars: List[Dict[str, Any]] = []
            for attempt in range(3):
                try:
                    data = await svc.kline(code, "daily", args.adjust, args.bars)
                    bars = data.get("bars") or []
                    break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.3 * (attempt + 1))
            if bars:
                store.put_bars(code, args.adjust, bars)
            else:
                store.mark_failed(code, args.adjust)
            async with lock:
                done += 1
                if not bars:
                    fail += 1
                if done % 100 == 0 or done == len(todo):
                    el = time.time() - t0
                    rate = done / max(el, 0.01)
                    print(
                        f"\r  {done}/{len(todo)}  失败{fail}({fail / done:.0%})  "
                        f"{rate:5.1f}/s  ETA {(len(todo) - done) / max(rate, 0.01):4.0f}s",
                        end="", flush=True,
                    )
                if done >= 40 and fail / done > args.max_fail_rate and not aborted:
                    aborted = True
                    print(f"\n  !! 失败率 {fail / done:.0%} 过高，疑似被限流，中止。"
                          f" 稍后重跑即可断点续传。")

        await asyncio.gather(*(work(c) for c in todo))
        print()

    print(f"\n完成：成功 {done - fail} 只 / 失败 {fail} 只 / 用时 {time.time() - t0:.0f}s")
    print("缓存:", store.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
