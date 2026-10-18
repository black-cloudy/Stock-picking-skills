"""研究步骤 28：把沪深主板 qfq 日线强制刷新到最新交易日。

背景：``ensure_bars`` 只判断"根数够不够"，老股票永远不重拉；而 ``warmup`` 刷的是
``none``（不复权）。本脚本用 ``force=True`` 把扫描实际使用的 ``qfq`` 数据拉新。

用法::

    python research/02_data/28_refresh_qfq.py
    python research/02_data/28_refresh_qfq.py --market all      # 含创业板/科创板
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.scanner import Screener  # noqa: E402


def keep(code: str, market: str) -> bool:
    if market == "main":
        return code.startswith(("000", "001", "002", "003",
                                "600", "601", "603", "605"))
    if market in ("sh", "sz"):
        return code.startswith(("6",) if market == "sh" else ("0", "3"))
    return not code.startswith(("4", "8", "920"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="main", choices=["main", "sh", "sz", "all"])
    ap.add_argument("--concurrency", type=int, default=24)
    ap.add_argument("--bars", type=int, default=400)
    args = ap.parse_args()

    async with Screener(concurrency=args.concurrency) as sc:
        items = await sc.universe()
        codes = [it["code"] for it in items if keep(it["code"], args.market)]
        print(f"目标：{args.market} 共 {len(codes)} 只，开始强制刷新 qfq 日线 …")
        t0 = time.time()
        got = await sc.ensure_bars(codes, "qfq", 120, "① 刷新 qfq", tail=args.bars, force=True)
        print(f"\n完成：成功 {len(got)} 只 / 目标 {len(codes)} 只，用时 {time.time() - t0:.0f}s")
        print("缓存:", sc.store.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
