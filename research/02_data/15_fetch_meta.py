"""研究步骤 15：抓取全市场元数据（流通股本 / 总股本 / 行业），供换手率等特征使用。

用法::

    python research/02_data/15_fetch_meta.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent import MarketService  # noqa: E402

ROOT = _p
OUT = os.path.join(ROOT, ".data", "meta.json")


async def main() -> int:
    async with MarketService() as svc:
        items = await svc.all_stocks("all")
    meta: Dict[str, Any] = {}
    for it in items:
        code = it.get("code")
        if not code:
            continue
        price = it.get("price") or 0
        fmv = it.get("float_mv") or 0
        tmv = it.get("total_mv") or 0
        meta[code] = {
            "name": it.get("name") or "",
            "float_shares": round(fmv / price, 0) if price else 0,   # 股
            "total_shares": round(tmv / price, 0) if price else 0,
            "float_mv": fmv,
            "total_mv": tmv,
            "industry": it.get("industry"),
        }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    n = sum(1 for v in meta.values() if v["float_shares"])
    print(f"已保存 {len(meta)} 只到 {OUT}（其中 {n} 只有流通股本）")
    for c in ("002068", "002708", "002194", "002498", "600176"):
        if c in meta:
            m = meta[c]
            print(f"  {c} {m['name']}: 流通股本 {m['float_shares'] / 1e8:.2f}亿股  "
                  f"流通市值 {m['float_mv'] / 1e8:.1f}亿  行业={m['industry']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
