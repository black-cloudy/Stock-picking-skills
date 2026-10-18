"""FastAPI 应用装配。"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import mrj_router, router, ws_router
from .config import Settings, get_settings
from .service import MarketService

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    app.state.settings = settings
    app.state.service = MarketService(settings)
    log.info("StockAgent 启动，数据源=%s", settings.primary_source)
    # 后台预热板块指数（首次构建需几秒，预热后首次扫描秒出）
    async def _prewarm() -> None:
        def _run() -> None:
            try:
                from .screener.context import get_context
                from .screener.scanner import default_store_path
                from .screener.store import ScreenerStore

                get_context(ScreenerStore(default_store_path(settings)))
                log.info("板块指数预热完成")
            except Exception as exc:  # noqa: BLE001
                log.warning("板块指数预热失败: %s", exc)

        await asyncio.to_thread(_run)

    _task = asyncio.create_task(_prewarm())
    try:
        yield
    finally:
        _task.cancel()
        await app.state.service.close()
        log.info("StockAgent 已关闭")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="StockAgent · A 股实时行情服务",
        description=(
            "免费、无需 token 的 A 股行情 API：实时报价、五档盘口、分时图、"
            "日/周/月/分钟 K 线、排行榜与搜索。\n\n"
            "数据源：东方财富（主）+ 新浪财经（备用）。"
        ),
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)
    app.include_router(ws_router)
    app.include_router(mrj_router)

    # ---- 静态前端 ----
    web_dir = settings.web_dir
    index_file = os.path.join(web_dir, "index.html")
    if os.path.isdir(web_dir):
        app.mount("/static", StaticFiles(directory=web_dir), name="static")

        @app.get("/", include_in_schema=False, response_model=None)
        async def index() -> Any:
            if os.path.isfile(index_file):
                return FileResponse(index_file)
            return JSONResponse({"detail": "web/index.html 不存在"}, status_code=404)

        @app.get("/mrj", include_in_schema=False, response_model=None)
        async def mrj_page() -> Any:
            page = os.path.join(web_dir, "mrj.html")
            if os.path.isfile(page):
                return FileResponse(page)
            return JSONResponse({"detail": "web/mrj.html 不存在"}, status_code=404)

        @app.get("/mrj/", include_in_schema=False, response_model=None)
        async def mrj_page_slash() -> Any:
            return await mrj_page()

    return app


app = create_app()
