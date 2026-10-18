"""命令行工具。

示例::

    python -m stock_agent.cli quote 600519
    python -m stock_agent.cli kline 600519 --period daily --limit 10
    python -m stock_agent.cli minute 600519
    python -m stock_agent.cli rank --market cyb --sort pct --page-size 10
    python -m stock_agent.cli search 茅台
    python -m stock_agent.cli watch 600519 000001 300750 --interval 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, List

from .service import MarketService


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def _print_quote(row: dict) -> None:
    pct = row.get("change_pct")
    arrow = "▲" if (pct or 0) > 0 else ("▼" if (pct or 0) < 0 else "—")
    print(
        f"{row.get('code')} {row.get('name')}  "
        f"{_fmt(row.get('price'))}  {arrow}{_fmt(pct)}%  "
        f"额:{_fmt((row.get('amount') or 0) / 1e8)}亿  "
        f"换手:{_fmt(row.get('turnover_rate'))}%"
    )


async def cmd_quote(svc: MarketService, args: argparse.Namespace) -> None:
    row = await svc.quote(args.code, with_orderbook=args.book)
    if args.json:
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return
    _print_quote(row)
    if args.book and row.get("orderbook"):
        book = row["orderbook"]
        print("\n  卖盘                          买盘")
        for i in range(4, -1, -1):
            ask = book["asks"][i]
            bid = book["bids"][i]
            print(
                f"  卖{i + 1} {_fmt(ask['price']):>10} {_fmt(ask['volume'], 0):>10}   |   "
                f"买{i + 1} {_fmt(bid['price']):>10} {_fmt(bid['volume'], 0):>10}"
            )


async def cmd_minute(svc: MarketService, args: argparse.Namespace) -> None:
    data = await svc.minute(args.code, args.days)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"{data['name']} ({data['code']})  昨收 {_fmt(data['pre_close'])}  共 {data['count']} 点")
    for bar in data["trends"][-args.tail :]:
        print(
            f"  {bar['time']}  {_fmt(bar['price']):>10}  均 {_fmt(bar['avg_price']):>10}  "
            f"量 {_fmt(bar['volume'], 0):>10}  {_fmt(bar['change_pct'])}%"
        )


async def cmd_kline(svc: MarketService, args: argparse.Namespace) -> None:
    data = await svc.kline(args.code, args.period, args.adjust, args.limit)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(
        f"{data['code']}  周期={data['period']}  复权={data['adjust']}  共 {data['count']} 根"
    )
    print(f"  {'日期':<12}{'开':>10}{'高':>10}{'低':>10}{'收':>10}{'涨跌%':>9}{'成交量(手)':>14}")
    for bar in data["bars"][-args.tail :]:
        print(
            f"  {bar['date']:<12}{_fmt(bar['open']):>10}{_fmt(bar['high']):>10}"
            f"{_fmt(bar['low']):>10}{_fmt(bar['close']):>10}"
            f"{_fmt(bar['change_pct']):>9}{_fmt(bar['volume'], 0):>14}"
        )


async def cmd_rank(svc: MarketService, args: argparse.Namespace) -> None:
    data = await svc.stock_list(args.page, args.page_size, args.sort, args.order, args.market)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"共 {data['total']} 只  （{data['market']} / 按 {data['sort_field']} {data['order']}）")
    for i, row in enumerate(data["items"], start=(args.page - 1) * args.page_size + 1):
        print(f"{i:>4}. ", end="")
        _print_quote(row)


async def cmd_search(svc: MarketService, args: argparse.Namespace) -> None:
    items = await svc.search(args.keyword, args.limit)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    for row in items:
        print(f"  {row['code']}  {row['name']}  [{row.get('type')}]  {row.get('secid')}")


async def cmd_indices(svc: MarketService, args: argparse.Namespace) -> None:
    for row in await svc.indices():
        print(f"  {row.get('index_name') or row['name']:<10}  ", end="")
        _print_quote(row)


async def cmd_status(svc: MarketService, args: argparse.Namespace) -> None:
    print(json.dumps(svc.market_status(), ensure_ascii=False, indent=2))


async def cmd_watch(svc: MarketService, args: argparse.Namespace) -> None:
    codes: List[str] = args.codes
    try:
        while True:
            rows = await svc.quotes(codes)
            status = svc.market_status()
            print(f"\n=== {status['now']}  [{status['label']}] ===")
            for row in rows:
                _print_quote(row)
            await asyncio.sleep(args.interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n已退出。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stock-agent", description="A 股行情命令行工具")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_json(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="输出原始 JSON")

    p = sub.add_parser("quote", help="实时行情")
    p.add_argument("code")
    p.add_argument("-b", "--book", action="store_true", help="显示五档盘口")
    add_json(p)
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("minute", help="分时图")
    p.add_argument("code")
    p.add_argument("-d", "--days", type=int, default=1, choices=range(1, 6))
    p.add_argument("--tail", type=int, default=20)
    add_json(p)
    p.set_defaults(func=cmd_minute)

    p = sub.add_parser("kline", help="K 线")
    p.add_argument("code")
    p.add_argument("-p", "--period", default="daily")
    p.add_argument("-a", "--adjust", default="qfq", choices=["none", "qfq", "hfq"])
    p.add_argument("-n", "--limit", type=int, default=30)
    p.add_argument("--tail", type=int, default=30)
    add_json(p)
    p.set_defaults(func=cmd_kline)

    p = sub.add_parser("rank", help="排行榜")
    p.add_argument("--market", default="all", choices=["all", "sh", "sz", "bj", "cyb", "kcb", "main", "index", "etf"])
    p.add_argument("--sort", default="pct")
    p.add_argument("--order", default="desc", choices=["desc", "asc"])
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--page-size", type=int, default=20)
    add_json(p)
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser("search", help="搜索")
    p.add_argument("keyword")
    p.add_argument("--limit", type=int, default=10)
    add_json(p)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("indices", help="大盘指数")
    p.set_defaults(func=cmd_indices)

    p = sub.add_parser("status", help="市场状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="轮询盯盘")
    p.add_argument("codes", nargs="+")
    p.add_argument("--interval", type=float, default=3.0)
    p.set_defaults(func=cmd_watch)

    return parser


async def _run(args: argparse.Namespace) -> None:
    async with MarketService() as svc:
        await args.func(svc, args)


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
