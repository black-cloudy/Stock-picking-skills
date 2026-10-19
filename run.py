"""启动入口。

用法::

    python run.py                      # 默认 http://127.0.0.1:8000，并自动打开 /mrj
    python run.py --page /mrj?mode=broad   # 启动后直接打开「广义美人肩」
    python run.py --port 9000 --reload
    python run.py --host 0.0.0.0
    python run.py --no-open            # 只启动服务，不开浏览器
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

# 允许直接 `python run.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_PAGE = "/mrj"


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
    parser.add_argument(
        "--page",
        default=os.getenv("STOCK_START_PAGE", DEFAULT_PAGE),
        help="启动后自动打开的页面路径（默认 /mrj；传空字符串则不打开）",
    )
    parser.add_argument(
        "--open", dest="auto_open", default=True, action="store_true",
        help="启动后自动打开浏览器（默认开启）",
    )
    parser.add_argument(
        "--no-open", dest="auto_open", action="store_false",
        help="启动后不打开浏览器",
    )
    return parser


def _open_when_ready(url: str, host: str, port: int, timeout: float = 60.0) -> None:
    """等端口真的 listen 了再开浏览器，避免刚启动时 Connection refused。"""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host

    def _run() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((probe_host, port), timeout=0.6):
                    webbrowser.open(url)
                    return
            except OSError:
                time.sleep(0.3)

    threading.Thread(target=_run, daemon=True).start()


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

    base = f"http://{args.host}:{args.port}"
    page = (args.page or "").strip()
    if page and not page.startswith("/"):
        page = "/" + page
    target = base + page if page else ""

    banner = f"""
  +--------------------------------------------------+
  |  StockAgent . A 股形态量化选股                    |
  |  Web 界面 : http://{args.host}:{args.port}/{' ' * max(0, 20 - len(args.host) - len(str(args.port)))}|
  |  接口文档 : http://{args.host}:{args.port}/docs{' ' * max(0, 16 - len(args.host) - len(str(args.port)))}|
  +--------------------------------------------------+
  |  美人肩页 : {base}/mrj
  |  经典/广义 : {base}/mrj?mode=classic  或  ?mode=broad
  |  低位票页 : {base}/mrj?mode=lowpos
  +--------------------------------------------------+
"""
    print(banner)
    if target and args.auto_open:
        print(f"  [浏览器] 准备打开 {target}（就绪后自动弹出，Ctrl+C 停止服务）\n")
        _open_when_ready(target, args.host, args.port)

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
