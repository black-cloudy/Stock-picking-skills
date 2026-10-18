"""冒烟测试：验证各数据接口能取到数据（需要联网）。

运行::

    python tests/test_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_agent.service import MarketService  # noqa: E402
from stock_agent.utils import (  # noqa: E402
    guess_market,
    market_phase,
    normalize_code,
    to_secid,
)

PASSED, FAILED = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [OK]   {name} {detail}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


def test_pure_functions() -> None:
    print("\n[1] 纯函数")
    check("normalize_code 600519", normalize_code("600519") == "600519")
    check("normalize_code sh600519", normalize_code("sh600519") == "600519")
    check("normalize_code 600519.SH", normalize_code("600519.SH") == "600519")
    check("normalize_code 1.600519", normalize_code("1.600519") == "600519")
    check("to_secid 600519 -> 1.x", to_secid("600519") == "1.600519")
    check("to_secid 000001 -> 0.x", to_secid("000001") == "0.000001")
    check("to_secid 300750 -> 0.x", to_secid("300750") == "0.300750")
    check("to_secid 688981 -> 1.x", to_secid("688981") == "1.688981")
    check("to_secid 830799 -> 0.x (北交所)", to_secid("830799") == "0.830799")
    check("guess_market 601318 == 1", guess_market("601318") == 1)
    check("market_phase 返回合法值", market_phase() in
          {"pre_open", "call_auction", "trading", "lunch_break", "closed", "weekend"})


async def test_network() -> None:
    print("\n[2] 网络数据源")
    async with MarketService() as svc:
        st = svc.market_status()
        print(f"       市场状态: {st['label']}  时间: {st['now']}  数据源: {st['sources']}")

        q = await svc.quote("600519", with_orderbook=True)
        check("实时行情", q.get("name") == "贵州茅台" and q.get("price") is not None,
              f"-> {q.get('name')} {q.get('price')} {q.get('change_pct')}%")
        book = q.get("orderbook") or {}
        check("五档盘口", len(book.get("bids", [])) == 5 and len(book.get("asks", [])) == 5,
              f"-> 买一 {book.get('bids', [{}])[0].get('price')} 卖一 {book.get('asks', [{}])[0].get('price')}")

        idx = await svc.indices()
        check("大盘指数", len(idx) >= 4, f"-> {len(idx)} 个，如 {idx[0]['name']} {idx[0]['price']}")

        minute = await svc.minute("000001")
        check("分时数据", minute["count"] > 0, f"-> {minute['name']} {minute['count']} 个点")

        minute5 = await svc.minute("000001", days=5)
        check("5 日分时", minute5["count"] > minute["count"] * 3,
              f"-> {minute5['count']} 个点")

        daily = await svc.kline("600519", "daily", "qfq", 60)
        check("日 K 线", daily["count"] == 60, f"-> {daily['count']} 根，最后一根 {daily['bars'][-1]['date']}")

        monthly = await svc.kline("600519", "monthly", "qfq", 60)
        check("月 K 线", monthly["count"] >= 12, f"-> {monthly['count']} 根，最后一根 {monthly['bars'][-1]['date']}")

        m5 = await svc.kline("600519", "5m", "qfq", 100)
        check("5 分钟 K 线", m5["count"] > 0, f"-> {m5['count']} 根")

        big = await svc.kline("600519", "daily", "qfq", 2000)
        check("日 K 翻页(请求2000根)", big["count"] >= 500,
              f"-> {big['count']} 根 (源={big.get('source', 'eastmoney')})")

        rows = await svc.quotes(["600519", "000001", "300750", "688981"])
        check("批量行情", len(rows) == 4, f"-> {[r['name'] for r in rows]}")

        rank = await svc.stock_list(page=1, page_size=10, sort_field="pct", market="all")
        check("全市场排行榜", rank["total"] > 4000 and len(rank["items"]) == 10,
              f"-> 共 {rank['total']} 只，榜首 {rank['items'][0]['name']} {rank['items'][0]['change_pct']}%")

        cyb = await svc.stock_list(page=1, page_size=5, market="cyb")
        check("创业板筛选", len(cyb["items"]) == 5,
              f"-> {[i['code'] for i in cyb['items']]}")

        found = await svc.search("茅台")
        check("搜索(名称)", any(i["code"] == "600519" for i in found),
              f"-> {[i['code'] + i['name'] for i in found[:3]]}")

        found2 = await svc.search("300750")
        check("搜索(代码)", any(i["code"] == "300750" for i in found2))

        ticks = await svc.ticks("600519", 20)
        check("逐笔成交", len(ticks) > 0, f"-> {len(ticks)} 笔")

        snap = await svc.snapshot("600519", period="daily", kline_limit=60, with_ticks=True)
        check("聚合快照", all(snap.get(k) for k in ("quote", "minute", "kline")),
              f"-> errors={snap.get('errors')}")


async def main() -> int:
    print("=" * 66)
    print(" StockAgent 冒烟测试")
    print("=" * 66)
    test_pure_functions()
    try:
        await test_network()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        FAILED.append(f"未捕获异常: {exc}")

    print("\n" + "=" * 66)
    print(f" 通过 {len(PASSED)} 项 / 失败 {len(FAILED)} 项")
    if FAILED:
        print(" 失败项: " + ", ".join(FAILED))
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
