"""美人肩 / 低位票 量化选股 Web API。

三种模式（``mode`` 参数）：

* ``classic`` —— **经典美人肩**：涨停 / 准涨停（≥9.4%）→ 横盘 4~8 天 → 二次涨停
* ``broad``   —— **广义美人肩**：大涨未封板（4%~9.4%）→ 横盘 3~8 天 → 涨停
* ``lowpos``  —— **低位票**：前期暴跌 → 均线低位粘合 → 拐头向上 / 放量突破

接口::

* ``GET /api/mrj/stats``      —— 三种模式的元信息 + 回测口径（头部展示用）
* ``GET /api/mrj/scan``       —— 按模式扫描候选股（带打分 / 特征 / 卡片文案）
* ``GET /api/mrj/detail``     —— 单只标的：K 线 + 指标 + 作战手册（按模式取不同口径）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..screener.indicators import macd, moving_average
from ..screener.lowpos import (
    LowPosConfig,
    detect_low_position,
    extract_lowpos,
    lowpos_checklist,
)
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

# ============================================================================
# 三种模式的元信息（回测数字来自各 Skill 的 30 万组合回测，不复权真实价格）
# ============================================================================

MODES: Dict[str, Dict[str, Any]] = {
    "classic": {
        "label": "经典美人肩",
        "tagline": "涨停/准涨停 → 横盘 → 二次涨停",
        "engine": "沪深A股 · 经典美人肩（涨停 → 横盘 → 二次涨停）扫描引擎",
        "condition": (
            "扫描条件：锚点涨幅 9.4%~10.5%（涨停/准涨停） · 横盘 4~8 天 · "
            "收盘震幅 ±3.5% · 日均振幅 ≤4% · 量能趋势 ≥0.9 · 沪深主板"
        ),
        "window": "最佳潜伏窗口：首板后第 4 ~ 8 个交易日",
        "desc": (
            "一只股票涨停后横盘数日、量能不放不缩、股价钉在首板价附近 —— 这是资金洗盘的典型特征。"
            "本系统扫描该形态，在<b>二次涨停爆发前</b>给出候选与潜伏区间。"
            "这是最严格的口径：相邻参数同样为正，稳健性最好，但年均只有约 30 次机会。"
        ),
        "metrics": [["+61.3%", "五年总收益"], ["12.6%", "最大回撤"], ["1.46", "盈亏比 PF"], ["45.8%", "胜率"]],
        "backtest": {
            "window": "2021-01 ~ 2026-10-16",
            "equity": "483,800",
            "total_return": 61.3,
            "max_dd": 12.6,
            "trades": 177,
            "win_rate": 45.8,
            "pf": 1.46,
        },
        "params": {"min_gap": 4, "max_gap": 8, "close_band": 0.035,
                   "anchor_min_gain": 9.4, "anchor_max_gain": 10.5,
                   "min_vol": 0.9, "max_amp": 4.0},
    },
    "broad": {
        "label": "广义美人肩",
        "tagline": "大涨未封板 → 横盘 → 涨停",
        "engine": "沪深A股 · 广义美人肩（大涨未封板 → 横盘 → 涨停）扫描引擎",
        "condition": (
            "扫描条件：锚点涨幅 4%~9.4%（大涨未封板） · 横盘 3~8 天 · "
            "收盘震幅 ±4% · 日均振幅 ≤4% · 量能趋势 ≥0.9 · 沪深主板"
        ),
        "window": "最佳潜伏窗口：锚点后第 3 ~ 8 个交易日",
        "desc": (
            "同一套爬山逻辑，只是「第一步」没那么猛：锚点是大涨 4%~9.4% 的强势阳线（<b>没封涨停</b>），"
            "之后横盘 3~8 天、平台重心不下移，然后再等一根涨停。"
            "信号量约为经典口径的 120 倍，收益更高但回撤也大（28%）—— 建议只作观察池、小仓位试。"
        ),
        "metrics": [["+122.0%", "五年总收益"], ["28.0%", "最大回撤"], ["1.16", "盈亏比 PF"], ["48.3%", "胜率"]],
        "backtest": {
            "window": "2021-01 ~ 2026-10-16",
            "equity": "666,088",
            "total_return": 122.0,
            "max_dd": 28.0,
            "trades": 741,
            "win_rate": 48.3,
            "pf": 1.16,
        },
        "params": {"min_gap": 3, "max_gap": 8, "close_band": 0.04,
                   "anchor_min_gain": 4.0, "anchor_max_gain": 9.4,
                   "min_vol": 0.9, "max_amp": 4.0},
    },
    "lowpos": {
        "label": "低位票",
        "tagline": "暴跌 → 均线粘合 → 拐头/突破",
        "engine": "沪深A股 · 低位票（暴跌 → 均线粘合 → 拐头 / 突破）扫描引擎",
        "condition": (
            "扫描条件：250 日回撤 ≥30% · 低位（250 日位置 ≤45%，突破档 ≤65%） · "
            "MA5/10/15/20 离散度 ≤2.5% · 拐头（5 日内金叉）或放量突破（≥4%） · 沪深主板"
        ),
        "window": "最佳潜伏窗口：金叉后第 0 ~ 5 个交易日（拐头档）",
        "desc": (
            "前期暴跌（250 日回撤 ≥30%）→ 在低位横盘、MA5/10/15/20 粘合在一起 → "
            "然后<b>拐头向上</b>（金叉）或<b>放量大阳/涨停突破</b>。"
            "真正有超额的是「拐头」档（买在启动前）；「突破」档是看图确认，机械追突破没有超额，别无脑买。"
        ),
        "metrics": [["+384.9%", "五年总收益"], ["31.4%", "年化收益"], ["19.2%", "最大回撤"], ["221", "交易笔数"]],
        "backtest": {
            "window": "2021-01 ~ 2026-10-16",
            "equity": "1,454,615",
            "total_return": 384.87,
            "max_dd": 19.21,
            "trades": 221,
            "annual": 31.41,
        },
        "params": {"mode": "both", "max_position": 65, "max_spread": 2.5,
                   "cross_days": 5, "breakout_min_gain": 4.0},
    },
}

MODE_KEYS = tuple(MODES)


def _norm_mode(mode: Optional[str]) -> str:
    m = (mode or "classic").strip().lower()
    return m if m in MODES else "classic"


def _f(v: Any, d: int = 2, suffix: str = "") -> str:
    if v is None:
        return "--"
    try:
        return f"{float(v):.{d}f}{suffix}"
    except (TypeError, ValueError):
        return "--"


def _tone(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    return "up" if x > 0 else ("dn" if x < 0 else "flat")


# ============================================================================
# 美人肩：配置 / 结果拍平
# ============================================================================


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


def _second_board_cfg(
    mode: str,
    min_gap: Optional[int] = None,
    max_gap: Optional[int] = None,
    close_band: Optional[float] = None,
    max_amp: Optional[float] = None,
    min_vol: Optional[float] = None,
) -> SecondBoardConfig:
    """按模式取预设口径，再用前端滑块做轻微覆盖。"""
    p = MODES[mode]["params"]
    cfg = _cfg(
        min_gap=int(p["min_gap"]),
        max_gap=int(p["max_gap"]),
        close_band=float(p["close_band"]),
        min_vol_trend=float(p["min_vol"]),
        anchor_min_gain=float(p["anchor_min_gain"]),
        anchor_max_gain=float(p["anchor_max_gain"]),
    )
    if mode == "broad":
        # 温和锚点分支：MACD 柱回落更宽容、量能下限略松（research/19_mild_anchor.py）
        cfg = replace(cfg, mild_min_vol_trend=0.6)
    cfg.max_avg_amp = float(p["max_amp"]) / 100.0
    cfg.min_platform_drift = -0.01

    if min_gap:
        cfg.min_gap = int(min_gap)
    if max_gap:
        cfg.max_gap = int(max_gap)
    if close_band:
        cfg.close_band = float(close_band)
    if max_amp is not None and max_amp > 0:
        cfg.max_avg_amp = float(max_amp) / 100.0
    if min_vol is not None and min_vol > 0:
        cfg.min_vol_trend = float(min_vol)
    return cfg


def _mrj_item(rec: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """把扫描结果拍平成前端友好的结构（卡片文案也在后端拼好）。"""
    gap = rec.get("整理天数")
    avg_amp = rec.get("平均振幅%")
    vol_trend = rec.get("量能趋势")
    score = rec.get("score")
    prob = round(float(rec.get("prob") or 0) * 100, 1)
    chg = rec.get("今日涨跌%")

    item = {
        "mode": mode,
        "code": rec.get("code"),
        "name": rec.get("name"),
        "industry": rec.get("行业") or "",
        "price": rec.get("现价"),
        "change_pct": chg,
        "score": score,
        "prob": prob,
        "gap": gap,
        "avg_amp": avg_amp,
        "vol_trend": vol_trend,
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
    }

    lu = (chg or 0) >= 9.8
    item["status"] = "二板已成" if lu else "临界爆发"
    if lu:
        note = "【二板已成】今日已封涨停，形态兑现"
    else:
        note = (f"【临界爆发】横盘第 {gap} 天，量能趋势 {_f(vol_trend)}"
                f"{'（放量）' if (vol_trend or 0) >= 1 else ''}")
    item["note"] = f"{note} · 二次涨停概率 <b>{_f(prob, 1)}%</b>"
    item["metrics"] = [
        {"value": f"{gap}", "label": "蓄势天数"},
        {"value": _f(avg_amp, 2, "%"), "label": "日均振幅"},
        {"value": _f(vol_trend, 2), "label": "量能趋势"},
        {"value": _f(score, 1), "label": "匹配得分"},
    ]
    item["raw"] = {k: v for k, v in rec.items() if k != "features"}
    return item


# ============================================================================
# 低位票：配置 / 结果拍平
# ============================================================================


def _lowpos_cfg(
    sub: str = "both",
    max_pos: Optional[float] = None,
    min_vol: Optional[float] = None,
) -> LowPosConfig:
    sub = (sub or "both").strip().lower()
    if sub not in ("both", "early", "breakout"):
        sub = "both"
    cfg = LowPosConfig(mode=sub)
    if max_pos and max_pos > 0:
        v = float(max_pos) / 100.0
        cfg.max_position = min(cfg.max_position, v)
        cfg.max_position_breakout = min(cfg.max_position_breakout, max(v, 0.65))
    if min_vol and min_vol > 0:
        cfg.min_vol_trend = float(min_vol)
    return cfg


def _lowpos_item(rec: Dict[str, Any]) -> Dict[str, Any]:
    sub = rec.get("模式") or "拐头"
    is_bk = sub == "突破"
    chg = rec.get("今日涨跌%")
    if chg is None:
        chg = rec.get("今日涨幅%")
    score = rec.get("score")
    pos = rec.get("250日位置%")
    spread = rec.get("均线离散度%")
    conv = rec.get("粘合天数")

    if is_bk:
        note = (f"【放量突破】今日 {_f(chg, 2, '%')} · 量比 {_f(rec.get('今日量比'))}"
                f" · 250 日位置 {_f(pos, 0, '%')}（看图确认，别追高）")
    else:
        note = (f"【拐头启动】金叉后第 {rec.get('金叉后天数', '--')} 天 · "
                f"BIAS20 {_f(rec.get('BIAS20%'), 2, '%')} · 250 日位置 {_f(pos, 0, '%')}")

    return {
        "mode": "lowpos",
        "code": rec.get("code"),
        "name": rec.get("name"),
        "industry": rec.get("行业") or "",
        "price": rec.get("现价"),
        "change_pct": chg,
        "score": score,
        "prob": None,
        "gap": conv,
        "avg_amp": spread,
        "vol_trend": rec.get("量比(5/20)"),
        "vol_ratio": rec.get("量比(5/20)"),
        "vol_last_ratio": rec.get("今日量比"),
        "close_pos": rec.get("250日位置%"),
        "range_pct": rec.get("均线离散度%"),
        "board_date": None,
        "board_price": rec.get("MA20"),
        "anchor_gain": None,
        "anchor_is_lu": False,
        "dev_close_last": rec.get("BIAS20%"),
        "dev_close_min": None,
        "dev_close_max": None,
        "dev_high_max": None,
        "dev_low_min": None,
        "boards_60d": None,
        "ma5": rec.get("MA5"),
        "ma10": rec.get("MA10"),
        "ma20": rec.get("MA20"),
        "ma_bull": bool(rec.get("MA5>MA10")) and (rec.get("MA10") or 0) >= (rec.get("MA20") or 0) * 0.99,
        "dif": rec.get("MACD_DIF"),
        "dea": rec.get("MACD_DEA"),
        "hist": rec.get("MACD柱"),
        "golden_cross": (rec.get("金叉后天数") or 99) <= 5,
        "decision_date": rec.get("决策日"),
        "suspended": 0,
        "status": "放量突破" if is_bk else "拐头启动",
        "note": note,
        "metrics": [
            {"value": _f(pos, 0, "%"), "label": "250日位置"},
            {"value": _f(spread, 2, "%"), "label": "均线离散度"},
            {"value": f"{conv}", "label": "粘合天数"},
            {"value": _f(score, 1), "label": "匹配得分"},
        ],
        "raw": rec,
    }


# ============================================================================
# /stats —— 三种模式的元信息 + 回测口径
# ============================================================================


@router.get("/stats", summary="选股模式与回测口径")
async def mrj_stats() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in MODES.items():
        item = dict(v)
        bt = v["backtest"]
        item["backtest_text"] = (
            f"五年实测（30 万本金）：期末 <b>{bt['equity']}</b> / "
            f"总收益 <b>{bt['total_return']:+.1f}%</b> / 最大回撤 <b>{bt['max_dd']:.1f}%</b>"
        )
        out[k] = item
    return {"ok": True, "modes": out, "default": "classic"}


# ============================================================================
# /scan —— 按模式扫描
# ============================================================================


@router.get("/scan", summary="按模式扫描候选股")
async def mrj_scan(
    mode: str = Query("classic", description="classic=经典美人肩 / broad=广义美人肩 / lowpos=低位票"),
    market: str = Query("main", description="all / main / sh / sz"),
    min_gap: Optional[int] = Query(None, ge=1, le=30),
    max_gap: Optional[int] = Query(None, ge=1, le=40),
    close_band: Optional[float] = Query(None, gt=0, le=0.2),
    max_amp: Optional[float] = Query(None, ge=0, le=20, description="整理期日均振幅上限%（0=不限）"),
    min_vol: Optional[float] = Query(None, ge=0, le=3, description="量能趋势下限"),
    sub: str = Query("both", description="低位票档位：both / early=拐头 / breakout=突破"),
    max_pos: Optional[float] = Query(None, ge=0, le=100, description="低位票：250 日位置上限%"),
    limit: int = Query(60, ge=1, le=200),
    refresh: int = Query(0, description="1=忽略结果缓存重扫（快，用本地数据）"),
    force_bars: int = Query(0, description="1=同时从上游重新拉取日线（慢，约 1~2 分钟）"),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    mode = _norm_mode(mode)
    key = (f"{mode}|{sub}|{market}|{min_gap}|{max_gap}|{close_band}|{max_amp}|"
           f"{min_vol}|{max_pos}|{limit}")
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

        t0 = time.time()
        hint = ""
        upstream = ""
        cov: Dict[str, Any] = {"date": "", "covered": 0, "total": 0}
        cov: Dict[str, Any] = {"date": "", "covered": 0, "total": 0}
        if mode == "lowpos":
            cfg = _lowpos_cfg(sub=sub, max_pos=max_pos, min_vol=min_vol)
            try:
                async with Screener(service=svc, concurrency=8) as sc:
                    rows = await sc.scan_low_position(
                        cfg, top=limit, market=market, adjust="qfq",
                        exclude_suspended=True, refresh=bool(force_bars),
                    )
                    cov = sc.store.date_coverage("qfq")
                    upstream = await sc.latest_trade_date()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"扫描失败: {exc}") from exc
            items = [_lowpos_item(r) for r in rows]
            items.sort(key=lambda x: -float(x["score"] or 0))
            if not items:
                hint = ("本地日线缓存里没有命中。若刚部署，请先预热缓存："
                        "python -m stock_agent.screener warmup")
        else:
            cfg = _second_board_cfg(
                mode, min_gap=min_gap, max_gap=max_gap,
                close_band=close_band, max_amp=max_amp, min_vol=min_vol,
            )
            try:
                async with Screener(service=svc, concurrency=8) as sc:
                    rows = await sc.scan_second_board(
                        cfg, top=limit, market=market, adjust="qfq",
                        exclude_suspended=True, refresh=bool(force_bars),
                    )
                    cov = sc.store.date_coverage("qfq")
                    upstream = await sc.latest_trade_date()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"扫描失败: {exc}") from exc
            items = [_mrj_item(r, mode) for r in rows]
            items.sort(key=lambda x: -float(x["score"] or 0))
            if not items and mode == "classic":
                hint = ("经典口径非常严格（只收涨停/准涨停锚点 + 4~8 天窄平台 + 量能不缩），"
                        "五年年均只有约 30 次机会 —— 今天没命中是正常的。"
                        "想多看几只可以拉大蓄势天数上限，或切到「广义美人肩」。")

        trade_date = cov.get("date") or ""
        covered, total_db = int(cov.get("covered") or 0), int(cov.get("total") or 0)
        covered_pct = (covered / total_db) if total_db else 0.0
        is_stale = bool(
            upstream and (not trade_date or trade_date < upstream or covered_pct < 0.9)
        )
        data = {
            "ok": True,
            "mode": mode,
            "mode_label": MODES[mode]["label"],
            "trade_date": trade_date,
            "upstream_date": upstream,
            "is_stale": is_stale,
            "coverage": {
                "date": trade_date, "covered": covered, "total": total_db,
                "pct": round(covered_pct * 100, 1),
            },
            "elapsed": round(time.time() - t0, 1),
            "total": len(items),
            "hint": hint,
            "params": {
                "mode": mode, "market": market, "min_gap": cfg.min_gap if mode != "lowpos" else None,
                "max_gap": cfg.max_gap if mode != "lowpos" else None,
                "close_band": cfg.close_band if mode != "lowpos" else None,
                "max_amp": max_amp, "min_vol": min_vol,
                "sub": sub if mode == "lowpos" else None,
                "max_pos": max_pos if mode == "lowpos" else None,
            },
            "items": items,
            "cached": False,
        }
        _CACHE[key] = {"ts": time.time(), "data": data}
        return data


# ============================================================================
# /detail —— 按模式给出 K 线 + 作战手册
# ============================================================================


async def _load_bars(svc: MarketService, code: str, tail: int = 260):
    async with Screener(service=svc, concurrency=4) as sc:
        try:
            await sc.ensure_bars([code], "qfq", 120, None, tail=tail)
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
    return raw, name


def _klines(raw: List[Dict[str, Any]], lo: int, hi: int) -> List[Dict[str, Any]]:
    return [
        {
            "date": raw[k].get("date"),
            "open": raw[k].get("open"), "close": raw[k].get("close"),
            "low": raw[k].get("low"), "high": raw[k].get("high"),
            "volume": raw[k].get("volume"),
        }
        for k in range(lo, hi)
    ]


async def _detail_second_board(code: str, mode: str, bars: int,
                               close_band: float, svc: MarketService) -> Dict[str, Any]:
    raw, name = await _load_bars(svc, code)

    ctx = StockContext(raw, code, name)
    n = ctx.n
    cfg = _second_board_cfg(mode)
    cfg.close_band = close_band

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
        raise HTTPException(status_code=404, detail=f"{code} 当前不符合{MODES[mode]['label']}形态")

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
    kline = _klines(raw, lo, n)
    dif, dea, hist = macd([ctx.closes[k] for k in range(n)])
    ma5 = moving_average(ctx.closes, 5)
    ma10 = moving_average(ctx.closes, 10)
    ma20 = moving_average(ctx.closes, 20)

    def _tail(arr: List[Any]) -> List[Any]:
        return [arr[k] for k in seg]

    price = ctx.closes[d2] or 0.0
    prev = ctx.closes[d2 - 1] if d2 > 0 else None
    chg = ((price - prev) / prev * 100) if prev else 0.0
    upper = round(price * 1.10, 2)                     # 二次涨停目标位
    stop = round(anchor_close * 0.93, 2)               # 严格止损：锚点价 × 0.93
    buy_lo = round(min(price, anchor_close) * 0.99, 2)
    buy_hi = round(max(price, anchor_close) * 1.01, 2)
    risk = max(price - stop, 0.01)
    rr = round((upper - price) / risk, 2)

    vol_trend = float(rec.get("量能趋势") or 0)
    checks = [
        ("锚点强势启动", bool(rec.get("锚点类型") == "涨停") or mode == "broad",
         f"锚点日涨幅 {rec.get('锚点涨幅%')}%（{rec.get('锚点类型')}）"),
        ("蓄势期振幅收敛", float(rec.get("range_pct") or 0) <= 0.15,
         f"整理期总振幅 {(float(rec.get('range_pct') or 0) * 100):.1f}%（上限 15%）"),
        ("收盘守住锚点价", float(rec.get("距首板价%") or 0) > -close_band * 100,
         f"距锚点价 {rec.get('距首板价%')}%"),
        ("量能撑住不缩量", vol_trend >= 0.9,
         f"量能趋势 {vol_trend:.2f}（≥0.9）"),
        ("均线多头排列", bool(rec.get("均线多头")),
         f"MA5 {rec.get('MA5')} / MA10 {rec.get('MA10')} / MA20 {rec.get('MA20')}"),
        ("MACD 零轴上方", bool((rec.get("MACD_DIF") or 0) > 0 and (rec.get("MACD_DEA") or 0) > 0),
         f"DIF {rec.get('MACD_DIF')} / DEA {rec.get('MACD_DEA')}"),
        ("60 日有涨停记忆", int(rec.get("60日涨停次数") or 0) >= 1,
         f"近 60 日涨停 {rec.get('60日涨停次数')} 次"),
        ("未跌破横盘下沿", price > stop, f"现价 {price:.2f} > 止损线 {stop:.2f}"),
    ]

    gap = rec.get("整理天数")
    note = (
        f"锚点日 {board_date}（{rec.get('锚点类型')} {rec.get('锚点涨幅%')}%）启动 → "
        f"横盘 <b>{gap}</b> 天蓄势，量能趋势 <b>{_f(vol_trend)}</b>，"
        f"收盘距锚点价 <b>{_f(rec.get('距首板价%'), 2, '%')}</b>，"
        f"整理期总振幅 <b>{_f((rec.get('range_pct') or 0) * 100, 1, '%')}</b>。"
    )

    return {
        "ok": True,
        "mode": mode,
        "code": code,
        "name": name,
        "head": {
            "sub": f"处于{MODES[mode]['label']}形态：锚点后第 <b>{gap}</b> 天横盘蓄势",
            "chips": [
                {"label": "MA5", "value": _f(rec.get("MA5"))},
                {"label": "MA10", "value": _f(rec.get("MA10"))},
                {"label": "MA20", "value": _f(rec.get("MA20"))},
                {"label": "匹配得分", "value": _f(rec.get("score"), 1)},
                {"label": "二次涨停概率", "value": _f(float(rec.get("prob") or 0) * 100, 1, "%")},
            ],
        },
        "stats": [
            {"label": "日期", "value": raw[d2].get("date") or "--"},
            {"label": "收盘", "value": _f(price), "tone": _tone(chg)},
            {"label": "涨跌", "value": f"{chg:+.2f}%", "tone": _tone(chg)},
            {"label": "日均振幅", "value": _f(rec.get("平均振幅%"), 2, "%")},
            {"label": "成交量", "value": f"{(raw[d2].get('volume') or 0) / 1e4:.1f}万"},
            {"label": "DIF", "value": _f(rec.get("MACD_DIF"), 3)},
            {"label": "DEA", "value": _f(rec.get("MACD_DEA"), 3)},
            {"label": "锚点价", "value": _f(anchor_close)},
        ],
        "kline": kline,
        "ma5": _tail(ma5), "ma10": _tail(ma10), "ma20": _tail(ma20),
        "dif": _tail(dif), "dea": _tail(dea), "hist": _tail(hist),
        "marks": {
            "point": {"index": idx - lo, "label": "首板" if mode == "classic" else "锚点"},
            "area": {"start": max(0, idx + 1 - lo), "end": n - 1 - lo, "label": "横盘蓄势区"},
        },
        # 兼容旧字段
        "anchor_index": idx - lo,
        "consolidation": [max(0, idx + 1 - lo), n - 1 - lo],
        "note": note,
        "plan": {
            "buy_range": [buy_lo, buy_hi],
            "stop": stop,
            "target": upper,
            "rr": rr,
            "anchor_price": round(anchor_close, 2),
            "anchor_date": board_date,
            "items": [
                {"kind": "buy", "label": "建仓潜伏区间",
                 "value": f"{_f(buy_lo)} ~ {_f(buy_hi)}", "hint": "锚点价附近低吸"},
                {"kind": "stop", "label": "严格止损线",
                 "value": _f(stop), "hint": "锚点价 × 0.93，破位离场"},
                {"kind": "target", "label": "二次涨停目标位",
                 "value": _f(upper), "hint": "现价 × 1.10"},
                {"kind": "rr", "label": "预期盈亏比",
                 "value": f"1 : {_f(rr, 1)}", "hint": "目标空间 / 止损空间"},
            ],
        },
        "checks": [{"label": a, "ok": b, "detail": c} for a, b, c in checks],
        "raw": {k: v for k, v in rec.items() if k != "features"},
    }


async def _detail_lowpos(code: str, sub: str, bars: int,
                         max_pos: Optional[float], svc: MarketService) -> Dict[str, Any]:
    raw, name = await _load_bars(svc, code, tail=360)
    cfg = _lowpos_cfg(sub=sub, max_pos=max_pos)

    keep = min(len(raw), max(cfg.min_bars + 60, 340))
    seg_bars = list(raw[-keep:])
    ctx = StockContext(seg_bars, code, name)
    n = ctx.n
    if n < cfg.min_bars:
        raise HTTPException(status_code=404, detail=f"{code} 历史数据不足（{n} < {cfg.min_bars} 根）")

    f = extract_lowpos(ctx, n - 1, cfg)
    if f is None:
        raise HTTPException(status_code=404, detail=f"{code} 无法计算低位特征")
    hit = detect_low_position(seg_bars, code, name, cfg)
    if hit is None:
        raise HTTPException(status_code=404, detail=f"{code} 当前不符合低位票形态")
    score = hit.get("score")

    is_bk = hit.get("模式") == "突破"
    kind = "breakout" if is_bk else "early"

    lo = max(0, n - bars)
    seg = list(range(lo, n))
    kline = _klines(seg_bars, lo, n)

    def _tail(arr: List[Any]) -> List[Any]:
        return [arr[k] for k in seg]

    price = float(f["price"])
    prev = ctx.closes[n - 2] if n >= 2 else None
    chg = ((price - prev) / prev * 100) if prev else 0.0
    ma5, ma10, ma20 = float(f["ma5"]), float(f["ma10"]), float(f["ma20"])

    lows20 = [x for x in ctx.lows[-20:] if x]
    lo20 = min(lows20) if lows20 else ma20 * 0.95
    buy_lo = round(min(ma20, price) * 0.99, 2)
    buy_hi = round(max(ma5, ma10) * 1.01, 2)
    if buy_hi < buy_lo:
        buy_lo, buy_hi = buy_hi, buy_lo
    stop = round(min(lo20 * 0.99, ma20 * 0.95), 2)
    target = round(price * 1.15, 2)          # 20 日持有参考目标
    risk = max(price - stop, 0.01)
    rr = round((target - price) / risk, 2)

    cross_idx = max(0, n - 1 - int(f["days_since_cross"]))
    area_end = n - 2 if is_bk else n - 1
    area_start = max(0, cross_idx - max(int(f["conv_days"]), 5))

    if is_bk:
        note = (f"前期 250 日回撤 <b>{_f(f['dd250'] * 100, 1, '%')}</b> → "
                f"均线在低位粘合（近 20 日最小离散度 <b>{_f(f['spread_min20'] * 100, 2, '%')}</b>）→ "
                f"今日<b>放量突破</b>：涨 <b>{f['chg1']:+.2f}%</b>，量比 <b>{_f(f['vol_today'])}</b>，"
                f"BIAS20 <b>{_f(f['bias20'], 2, '%')}</b>。追突破没有超额，建议等回踩。")
        point = {"index": n - 1 - lo, "label": "突破日"}
    else:
        note = (f"前期 250 日回撤 <b>{_f(f['dd250'] * 100, 1, '%')}</b> → "
                f"均线在低位粘合（近 10 日最小离散度 <b>{_f(f['spread_min10'] * 100, 2, '%')}</b>，"
                f"已粘合 <b>{int(f['conv_days'])}</b> 天）→ "
                f"MA5 刚上穿 MA10（第 <b>{int(f['days_since_cross'])}</b> 天），"
                f"MACD 柱 <b>{_f(f['macd_hist'], 3)}</b>。属于启动前 / 启动第一天。")
        point = {"index": cross_idx - lo, "label": "金叉"}

    return {
        "ok": True,
        "mode": "lowpos",
        "code": code,
        "name": name,
        "head": {
            "sub": ("已<b>放量突破</b>粘合平台（追高风险）" if is_bk
                    else "低位均线粘合后<b>刚拐头向上</b>（启动前）"),
            "chips": [
                {"label": "MA5", "value": _f(ma5)},
                {"label": "MA10", "value": _f(ma10)},
                {"label": "MA20", "value": _f(ma20)},
                {"label": "匹配得分", "value": _f(score, 1)},
                {"label": "250日位置", "value": _f(f["pos250"] * 100, 0, "%")},
            ],
        },
        "stats": [
            {"label": "日期", "value": seg_bars[-1].get("date") or "--"},
            {"label": "收盘", "value": _f(price), "tone": _tone(chg)},
            {"label": "涨跌", "value": f"{chg:+.2f}%", "tone": _tone(chg)},
            {"label": "均线离散度", "value": _f(f["spread_now"] * 100, 2, "%")},
            {"label": "成交量", "value": f"{(seg_bars[-1].get('volume') or 0) / 1e4:.1f}万"},
            {"label": "DIF", "value": _f(f["macd_dif"], 3)},
            {"label": "DEA", "value": _f(f["macd_dea"], 3)},
            {"label": "BIAS20", "value": _f(f["bias20"], 2, "%")},
        ],
        "kline": kline,
        "ma5": _tail(ctx.ma5), "ma10": _tail(ctx.ma10), "ma20": _tail(ctx.ma20),
        "dif": _tail(ctx.dif), "dea": _tail(ctx.dea), "hist": _tail(ctx.hist),
        "marks": {
            "point": point,
            "area": {"start": area_start - lo, "end": area_end - lo, "label": "均线粘合平台"},
        },
        "anchor_index": point["index"],
        "consolidation": [area_start - lo, area_end - lo],
        "note": note,
        "plan": {
            "buy_range": [buy_lo, buy_hi],
            "stop": stop,
            "target": target,
            "rr": rr,
            "anchor_price": round(ma20, 2),
            "anchor_date": None,
            "items": [
                {"kind": "buy", "label": "低吸参考区间",
                 "value": f"{_f(buy_lo)} ~ {_f(buy_hi)}", "hint": "回踩粘合均线低吸"},
                {"kind": "stop", "label": "严格止损线",
                 "value": _f(stop), "hint": "跌破平台下沿（近 20 日最低）离场"},
                {"kind": "target", "label": "20 日参考目标",
                 "value": _f(target), "hint": "现价 × 1.15"},
                {"kind": "rr", "label": "预期盈亏比",
                 "value": f"1 : {_f(rr, 1)}", "hint": "目标空间 / 止损空间"},
            ],
        },
        "checks": lowpos_checklist(f, cfg, kind),
        "raw": {k: v for k, v in f.items()},
    }


@router.get("/detail", summary="单只标的：K线 + 指标 + 作战手册")
async def mrj_detail(
    code: str = Query(..., description="6 位股票代码"),
    mode: str = Query("classic", description="classic / broad / lowpos"),
    bars: int = Query(70, ge=30, le=160),
    close_band: float = Query(0.07, gt=0, le=0.2),
    sub: str = Query("both", description="低位票档位：both / early / breakout"),
    max_pos: Optional[float] = Query(None, ge=0, le=100),
    svc: MarketService = ServiceDep,
) -> Dict[str, Any]:
    mode = _norm_mode(mode)
    code = code.strip()
    if mode == "lowpos":
        return await _detail_lowpos(code, sub, bars, max_pos, svc)
    return await _detail_second_board(code, mode, bars, close_band, svc)
