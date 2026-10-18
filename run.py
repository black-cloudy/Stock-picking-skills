"""启动入口。

用法::

    python run.py                      # 默认 http://127.0.0.1:8000
    python run.py --port 9000 --reload
    python run.py --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# 允许直接 `python run.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="StockAgent · A 股实时行情服务")
    parser.add_argument("--host", default=os.getenv("STOCK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("STOCK_PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="代码热重载（开发用）")
    parser.add_argument(
        "--log-level",
        default=os.getenv("STOCK_LOG_LEVEL", "info"),
        choices=["critical", "error", "warning", "info", "debug"],
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # 让配置读取到命令行参数
    os.environ["STOCK_HOST"] = args.host
    os.environ["STOCK_PORT"] = str(args.port)

    import uvicorn

    banner = f"""
  +--------------------------------------------------+
  |  StockAgent . A 股实时行情服务                   |
  |  Web 界面 : http://{args.host}:{args.port}/{' ' * max(0, 20 - len(args.host) - len(str(args.port)))}|
  |  接口文档 : http://{args.host}:{args.port}/docs{' ' * max(0, 16 - len(args.host) - len(str(args.port)))}|
  +--------------------------------------------------+
"""
    print(banner)

    uvicorn.run(
        "stock_agent.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        ws="auto",
    )


if __name__ == "__main__":
    main()
