"""FastAPI 路由定义。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from ..config import Settings, get_settings
from ..providers import DataSourceError
from ..service import MarketService
from ..utils import normalize_code

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["market"])
ws_router = APIRouter()


# ---------------------------------------------------------------- 依赖注入


def get_service(request: Request) -> MarketService:
    service = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - 正常启动不会发生
        raise HTTPException(status_code=503, detail="服务尚未初始化")
    return service


ServiceDep = Depends(get_service)
SettingsDep = Depends(get_settings)


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, (ValueError, DataSourceError)):
        return HTTPException(status_code=400, detail=str(exc))
    log.exception("接口异常")
    return HTTPException(status_code=502, detail=f"上游数据源异常: {exc}")


# ---------------------------------------------------------------- 基础


@router.get("/health", summary="健康检查")
async def health(settings: Settings = SettingsDep) -> Dict[str, Any]:
    return {
        "ok": True,
        "source": settings.primary_source,
        "sina_fallback": settings.enable_sina_fallback,
    }


@router.get("/market/status", summary="市场交易状态 / 缓存统计")
async def market_status(svc: MarketService = ServiceDep) -> Dict[str, Any]:
    return svc.market_status()


@router.get("/market/indices", summary="大盘指数实时行情")
async def indices(svc: MarketService = ServiceDep) -> Dict[str, Any]:
    try:
        return {"items": await svc.indices()}
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


# ---------------------------------------------------------------- 列表 / 搜索


@router.get("/stocks", summary="股票列表 / 排行榜")
async def stock_list(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    sort_field: str = Query("pct", description="排序字段: pct/price/amount/turnover/volume_ratio/main_inflow/total_mv/..."),
    order: str = Query("desc", pattern="^(desc|asc)$"),
    market: str = Query("all", description="all/sh/sz/bj/cyb/kcb/main/index/etf"),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    try:
        return await svc.stock_list(page, page_size, sort_field, order, market)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.get("/search", summary="搜索证券（代码/名称/拼音）")
async def search(
    q: str = Query(..., min_length=1, description="关键词"),
    limit: int = Query(10, ge=1, le=50),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    try:
        return {"items": await svc.search(q, limit)}
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.get("/batch/quotes", summary="批量实时行情")
async def batch_quotes(
    codes: str = Query(..., description="逗号分隔，如 600519,000001,300750"),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    items = [c.strip() for c in codes.split(",") if c.strip()]
    if not items:
        raise HTTPException(status_code=400, detail="codes 不能为空")
    if len(items) > 200:
        raise HTTPException(status_code=400, detail="单次最多 200 只")
    try:
        return {"items": await svc.quotes(items)}
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


# ---------------------------------------------------------------- 单只标的


@router.get("/stock/{code}/quote", summary="实时行情（可选五档）")
async def stock_quote(
    code: str,
    orderbook: bool = Query(False, description="是否附带五档盘口"),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    try:
        return await svc.quote(normalize_code(code), with_orderbook=orderbook)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.get("/stock/{code}/orderbook", summary="五档盘口")
async def stock_orderbook(code: str, svc: MarketService = ServiceDep) -> Dict[str, Any]:
    try:
        book = await svc.orderbook(normalize_code(code))
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    if book is None:
        raise HTTPException(status_code=404, detail="暂无盘口数据")
    return book


@router.get("/stock/{code}/minute", summary="分时图数据（1~5 日）")
async def stock_minute(
    code: str,
    days: int = Query(1, ge=1, le=5),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    try:
        return await svc.minute(normalize_code(code), days)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.get("/stock/{code}/kline", summary="K 线数据")
async def stock_kline(
    code: str,
    period: str = Query("daily", description="1m/5m/15m/30m/60m/daily/weekly/monthly"),
    adjust: str = Query("qfq", pattern="^(none|qfq|hfq)$"),
    limit: int = Query(250, ge=1, le=5000),
    start: Optional[str] = Query(None, description="起始日期 YYYYMMDD"),
    end: Optional[str] = Query(None, description="结束日期 YYYYMMDD"),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    try:
        return await svc.kline(normalize_code(code), period, adjust, limit, start, end)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.get("/stock/{code}/ticks", summary="逐笔成交明细")
async def stock_ticks(
    code: str,
    limit: int = Query(60, ge=1, le=500),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    try:
        return {"items": await svc.ticks(normalize_code(code), limit)}
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.get("/stock/{code}/snapshot", summary="聚合快照（行情+分时+K线）")
async def stock_snapshot(
    code: str,
    period: str = Query("daily"),
    adjust: str = Query("qfq", pattern="^(none|qfq|hfq)$"),
    kline_limit: int = Query(250, ge=1, le=5000),
    minute_days: int = Query(1, ge=1, le=5),
    orderbook: bool = Query(True),
    ticks: bool = Query(False),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    try:
        return await svc.snapshot(
            normalize_code(code),
            period=period,
            adjust=adjust,
            kline_limit=kline_limit,
            minute_days=minute_days,
            with_orderbook=orderbook,
            with_ticks=ticks,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


# ---------------------------------------------------------------- 实时推送


@ws_router.websocket("/ws/quotes")
async def ws_quotes(websocket: WebSocket) -> None:
    """订阅式实时行情推送。

    客户端发送::

        {"action": "subscribe", "codes": ["600519", "000001"]}

    服务端按 ``STOCK_WS_INTERVAL`` 推送::

        {"type": "quotes", "ts": "...", "items": [...]}
    """
    await websocket.accept()
    svc: MarketService = websocket.app.state.service
    settings: Settings = get_settings()

    codes: List[str] = []
    paused = False

    async def sender() -> None:
        while True:
            await asyncio.sleep(settings.ws_interval)
            if paused or not codes:
                continue
            try:
                rows = await svc.quotes(codes[: settings.ws_max_codes])
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "quotes",
                            "market_status": svc.market_status()["label"],
                            "items": rows,
                        },
                        ensure_ascii=False,
                    )
                )
            except WebSocketDisconnect:
                return
            except Exception as exc:  # noqa: BLE001
                try:
                    await websocket.send_text(
                        json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)
                    )
                except Exception:  # noqa: BLE001
                    return

    sender_task = asyncio.create_task(sender())
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            action = msg.get("action")
            if action == "subscribe":
                codes = [normalize_code(c) for c in (msg.get("codes") or [])][
                    : settings.ws_max_codes
                ]
                paused = False
                await websocket.send_text(
                    json.dumps({"type": "subscribed", "codes": codes}, ensure_ascii=False)
                )
            elif action == "pause":
                paused = True
                await websocket.send_text(json.dumps({"type": "paused"}, ensure_ascii=False))
            elif action == "resume":
                paused = False
                await websocket.send_text(json.dumps({"type": "resumed"}, ensure_ascii=False))
            elif action == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
