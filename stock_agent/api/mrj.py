"""美人肩（二次涨停）量化选股 Web API。

把 ``stock_agent.screener`` 的美人肩策略暴露成前端可用的 JSON 接口：

* ``GET /api/mrj/scan``      —— 扫描当前符合形态的候选股（带打分 / 概率 / 特征）
* ``GET /api/mrj/detail``    —— 单只标的的 K 线 + 指标 + 作战手册
* ``GET /api/mrj/stats``     —— 五年回测口径的统计（头部展示用）
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..screener.indicators import macd, moving_average
from ..screener.patterns import (
    SecondBoardConfig,
    StockContext,
    detect_second_board,
)
from ..screener.scanner import Screener
from ..service import MarketService

router = APIRouter(prefix="/api/mrj", tags=["美人肩"])


def get_service(request: Request) -> MarketService:
    svc = getattr(request.app.state, "service", None)
    if svc is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="服务尚未初始化")
    return svc


ServiceDep = Depends(get_service)

# ---- 扫描结果缓存（同一组参数 5 分钟内复用，避免重复网络请求）----
_CACHE: Dict[str, Any] = {}
_CACHE_TTL = 300.0
_LOCK = asyncio.Lock()

# 五年回测口径（research/24_backtest.py + research/27_holdout.py 的结果）
BACKTEST = {
    "window": "2021-09-17 ~ 2026-09-17",
    "selected": 8316,          # 五年被选中的横盘事件（去重）
    "hits": 1447,              # 其中最终兑现二次涨停
    "hit_rate": 17.4,          # 持有到被剔除的最终涨停率 %
    "signal_precision": 10.7,  # 信号级 3 日内涨停率 %
    "recall": 20.3,            # 召回率 %
    "per_day": 14.2,           # 平均每日候选
    "break_up": 26.0,          # 冲高突破 +7% 未封板
    "break_down": 39.5,        # 跌破下沿
    "timeout": 17.1,           # 超时
    "years": {"2021": 10.0, "2022": 10.7, "2023": 10.4,
              "2024": 9.7, "2025": 10.0, "2026": 12.3},
}


def _cfg(
    min_gap: int = 3,
    max_gap: int = 15,
    close_band: float = 0.07,
    max_range: float = 0.15,
    min_vol_trend: float = 0.8,
    anchor_min_gain: float = 9.5,
    anchor_max_gain: float = 10.5,
) -> SecondBoardConfig:
    return SecondBoardConfig(
        min_gap=min_gap,
        max_gap=max_gap,
        close_band=close_band,
        max_range_pct=max_range,
        min_vol_trend=min_vol_trend,
        anchor_min_gain=anchor_min_gain,
        anchor_max_gain=anchor_max_gain,
    )


def _item(rec: Dict[str, Any]) -> Dict[str, Any]:
    """把扫描结果拍平成前端友好的结构。"""
    return {
        "code": rec.get("code"),
        "name": rec.get("name"),
        "industry": rec.get("行业") or "",
        "price": rec.get("现价"),
        "change_pct": rec.get("今日涨跌%"),
        "score": rec.get("score"),
        "prob": round(float(rec.get("prob") or 0) * 100, 1),
        "gap": rec.get("整理天数"),
        "avg_amp": rec.get("平均振幅%"),
        "vol_trend": rec.get("量能趋势"),
        "vol_ratio": rec.get("整理期量比"),
        "vol_last_ratio": rec.get("决策日量比"),
        "close_pos": rec.get("收盘位置"),
        "range_pct": round(float(rec.get("range_pct") or 0) * 100, 1),
        "board_date": rec.get("首板日"),
        "board_price": rec.get("首板价"),
        "anchor_gain": rec.get("锚点涨幅%"),
        "anchor_is_lu": rec.get("锚点类型") == "涨停",
        "dev_close_last": rec.get("距首板价%"),
        "dev_close_min": rec.get("收盘下偏%"),
        "dev_close_max": rec.get("收盘上偏%"),
        "dev_high_max": rec.get("整理期最高偏离%"),
        "dev_low_min": rec.get("整理期最低偏离%"),
        "boards_60d": rec.get("60日涨停次数"),
        "ma5": rec.get("MA5"),
        "ma10": rec.get("MA10"),
        "ma20": rec.get("MA20"),
        "ma_bull": rec.get("均线多头"),
        "dif": rec.get("MACD_DIF"),
        "dea": rec.get("MACD_DEA"),
        "hist": rec.get("MACD柱"),
        "golden_cross": rec.get("MACD金叉"),
        "decision_date": rec.get("决策日"),
        "suspended": rec.get("停牌天数") or 0,
        "status": "临界爆发",
    }


@router.get("/stats", summary="美人肩策略统计（回测口径）")
async def mrj_stats() -> Dict[str, Any]:
    return {"ok": True, "backtest": BACKTEST}


@router.get("/scan", summary="扫描美人肩候选股")
async def mrj_scan(
    market: str = Query("main", description="all / main / sh / sz"),
    min_gap: int = Query(3, ge=1, le=30),
    max_gap: int = Query(15, ge=1, le=40),
    close_band: float = Query(0.07, gt=0, le=0.2),
    max_amp: float = Query(0.0, ge=0, le=20, description="整理期日均振幅上限%（0=不限）"),
    min_vol: float = Query(0.8, ge=0, le=3, description="量能趋势下限"),
    anchor_min_gain: float = Query(9.5),
    limit: int = Query(40, ge=1, le=200),
    refresh: int = Query(0, description="1=忽略结果缓存重扫（快，用本地数据）"),
    force_bars: int = Query(0, description="1=同时从上游重新拉取日线（慢，约 1~2 分钟）"),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    key = f"{market}|{min_gap}|{max_gap}|{close_band}|{max_amp}|{min_vol}|{anchor_min_gain}|{limit}"
    now = time.time()
    if not refresh:
        hit = _CACHE.get(key)
        if hit and now - hit["ts"] < _CACHE_TTL:
            out = dict(hit["data"])
            out["cached"] = True
            out["cache_age"] = round(now - hit["ts"], 1)
            return out

    async with _LOCK:
        # 双重检查，避免并发重复扫描
        if not refresh:
            hit = _CACHE.get(key)
            if hit and time.time() - hit["ts"] < _CACHE_TTL:
                out = dict(hit["data"])
                out["cached"] = True
                return out

        cfg = _cfg(min_gap=min_gap, max_gap=max_gap, close_band=close_band,
                   min_vol_trend=min_vol, anchor_min_gain=anchor_min_gain)
        if max_amp > 0:
            cfg.min_avg_amp = max_amp / 100.0

        t0 = time.time()
        try:
            async with Screener(service=svc, concurrency=16) as sc:
                rows = await sc.scan_second_board(
                    cfg, top=limit, market=market, adjust="qfq",
                    exclude_suspended=True, refresh=bool(force_bars),
                )
                trade_date = (sc.store.recent_dates("qfq", 1) or [""])[0]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"扫描失败: {exc}") from exc

        items = [_item(r) for r in rows]
        # 涨幅接近涨停价（≥9.8%）视为已封板；其余为横盘待爆发。统一按打分降序。
        for it in items:
            it["status"] = "二板已成" if (it.get("change_pct") or 0) >= 9.8 else "临界爆发"
        items.sort(key=lambda x: -float(x["score"] or 0))

        data = {
            "ok": True,
            "trade_date": trade_date,
            "elapsed": round(time.time() - t0, 1),
            "total": len(items),
            "params": {"market": market, "min_gap": min_gap, "max_gap": max_gap,
                       "close_band": close_band, "max_amp": max_amp,
                       "min_vol": min_vol, "anchor_min_gain": anchor_min_gain},
            "items": items,
            "cached": False,
        }
        _CACHE[key] = {"ts": time.time(), "data": data}
        return data


@router.get("/detail", summary="单只标的：K线 + 指标 + 作战手册")
async def mrj_detail(
    code: str = Query(..., description="6 位股票代码"),
    bars: int = Query(70, ge=30, le=160),
    close_band: float = Query(0.07, gt=0, le=0.2),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    code = code.strip()
    async with Screener(service=svc, concurrency=4) as sc:
        try:
            await sc.ensure_bars([code], "qfq", 120, None, tail=260)
        except Exception:  # noqa: BLE001
            pass
        raw = sc.store.get_bars(code, "qfq")
        name = sc.store.get_kv(f"name:{code}") or ""
        if not name:
            try:
                uni = await sc.universe()
                name = next((u.get("name") or "" for u in uni if u["code"] == code), "")
            except Exception:  # noqa: BLE001
                name = ""

    if not raw:
        raise HTTPException(status_code=404, detail=f"没有 {code} 的日线数据")

    ctx = StockContext(raw, code, name)
    n = ctx.n
    cfg = _cfg(close_band=close_band)

    # 从最近往前找最近一次形态（决策日=最新）
    rec: Optional[Dict[str, Any]] = None
    for tail in range(0, 12):
        sub_n = n - tail
        if sub_n < cfg.min_bars:
            break
        sub = raw[:sub_n]
        r = detect_second_board(sub, code, name, cfg, None)
        if r:
            rec = r
            ctx = StockContext(sub, code, name)
            n = ctx.n
            break
    if rec is None:
        raise HTTPException(status_code=404, detail=f"{code} 当前不符合美人肩形态")

    # ---- 找锚点下标与整理窗口 ----
    board_date = rec.get("首板日")
    idx = next((k for k in range(n - 1, -1, -1)
                if str(raw[k].get("date")) == str(board_date)), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="锚点定位失败")
    d2 = n - 1
    anchor_close = ctx.closes[idx] or 0.0

    lo = max(0, n - bars)
    seg = list(range(lo, n))
    kline = []
    for k in seg:
        b = raw[k]
        kline.append({
            "date": b.get("date"),
            "open": b.get("open"), "close": b.get("close"),
            "low": b.get("low"), "high": b.get("high"),
            "volume": b.get("volume"),
        })
    dif, dea, hist = macd([ctx.closes[k] for k in range(n)])
    ma5 = moving_average(ctx.closes, 5)
    ma10 = moving_average(ctx.closes, 10)
    ma20 = moving_average(ctx.closes, 20)

    def _tail(arr: List[Any]) -> List[Any]:
        return [arr[k] for k in seg]

    price = ctx.closes[d2] or 0.0
    prev = ctx.closes[d2 - 1] if d2 > 0 else None
    chg = ((price - prev) / prev * 100) if prev else 0.0
    upper = round(price * 1.10, 2)              # 二次涨停目标位
    stop = round(anchor_close * (1 - close_band), 2)   # 严格止损线
    buy_lo = round(anchor_close * (1 + min(0.0, float(rec.get("收盘下偏%") or 0) / 100)), 2)
    buy_hi = round(min(price, anchor_close * (1 + abs(close_band) * 0.15)), 2)
    risk = max(price - stop, 0.01)
    rr = round((upper - price) / risk, 2)

    vol_trend = float(rec.get("量能趋势") or 0)
    checks = [
        ("首板涨停启动", bool(rec.get("锚点类型") == "涨停"),
         f"锚点日涨幅 {rec.get('锚点涨幅%')}%"),
        ("蓄势期振幅收敛", float(rec.get("range_pct") or 0) <= 0.15,
         f"整理期总振幅 {(float(rec.get('range_pct') or 0) * 100):.1f}%（上限 15%）"),
        ("收盘守住锚点价", float(rec.get("距首板价%") or 0) > -close_band * 100,
         f"距锚点价 {rec.get('距首板价%')}%"),
        ("量能撑住不缩量", vol_trend >= 0.8,
         f"量能趋势 {vol_trend:.2f}（≥0.8）"),
        ("均线多头排列", bool(rec.get("均线多头")),
         f"MA5 {rec.get('MA5')} / MA10 {rec.get('MA10')} / MA20 {rec.get('MA20')}"),
        ("MACD 零轴上方", bool((rec.get("MACD_DIF") or 0) > 0 and (rec.get("MACD_DEA") or 0) > 0),
         f"DIF {rec.get('MACD_DIF')} / DEA {rec.get('MACD_DEA')}"),
        ("60 日有涨停记忆", int(rec.get("60日涨停次数") or 0) >= 1,
         f"近 60 日涨停 {rec.get('60日涨停次数')} 次"),
        ("未跌破横盘下沿", price > stop, f"现价 {price:.2f} > 止损线 {stop:.2f}"),
    ]

    return {
        "ok": True,
        "code": code,
        "name": name,
        "kline": kline,
        "ma5": _tail(ma5), "ma10": _tail(ma10), "ma20": _tail(ma20),
        "dif": _tail(dif), "dea": _tail(dea), "hist": _tail(hist),
        # 锚点与整理窗口在 kline 数组中的下标
        "anchor_index": idx - lo,
        "consolidation": [max(0, idx + 1 - lo), n - 1 - lo],
        "detail": {
            "date": raw[d2].get("date"),
            "close": round(price, 2),
            "change_pct": round(chg, 2),
            "amplitude": round((float(rec.get("平均振幅%") or 0)), 2),
            "volume": raw[d2].get("volume"),
            "dif": rec.get("MACD_DIF"),
            "dea": rec.get("MACD_DEA"),
            "turnover": rec.get("换手率%"),
            "score": rec.get("score"),
            "prob": round(float(rec.get("prob") or 0) * 100, 1),
            "gap": rec.get("整理天数"),
        },
        "plan": {
            "buy_range": [buy_lo, buy_hi],
            "stop": stop,
            "target": upper,
            "rr": rr,
            "anchor_price": round(anchor_close, 2),
            "anchor_date": board_date,
        },
        "checks": [{"label": a, "ok": b, "detail": c} for a, b, c in checks],
        "raw": {k: v for k, v in rec.items() if k != "features"},
    }
