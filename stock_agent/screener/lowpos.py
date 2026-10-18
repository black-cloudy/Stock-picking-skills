"""低位均线粘合票（低位的「均线粘合 → 拐头向上」）—— 特征提取与判定。

形态定义
--------
用户 2026-09 提供的两个样本：

* ``605366 宏柏新材`` @2026-09-14：从 14.90 跌到 6.39（-57%），低位横盘后
  MA5/10/15/20 粘在一起（近 10 日最小离散度 0.65%），MA5 刚上穿 MA10，
  MACD 柱翻红，其后 5 日最高 +7.1%。
* ``002990 盛视科技`` @2026-09-16：从 81.62 跌到 37.76（-54%），回踩后再横盘，
  四线离散度收敛到 0.36%，MA5 刚上穿 MA10，其后 5 日最高 +15.7%。

::

    前期暴跌（250 日区间回撤 ≥ 30%，且低点新鲜）
      → 在低位（250 日区间下半部）多根均线粘合（MA5/10/15/20 离散度 ≤ 2.5%）
      → 均线拐头向上（MA5 上穿 MA10、收盘站上均线、MACD 柱 ≥ 0）
      → 决策日（我们在这里选出来）

与研究口径一致（``research/28_lowpos_study.py``）：
默认档同时命中用户两个样本（605366 命中 09-11~09-17，002990 命中 09-16~09-18）。

⚠️ 诚实结论（2020-01 ~ 2026-09，沪深主板 3450 只，1613 个交易日）：

* 20 日预期收益 **+3.57%**（全市场基准 +1.85%、低位基准 +2.89%）；
* 20 日涨率 56.2%（基准 51.5%）；摸到 +15% 概率 30.5%（基准 27.2%）；
* 20 日平均最大回撤 **−6.9%**（基准 −8.1%）—— 回撤比大盘浅；
* 加 ``--min-vol-trend 1.1`` 可收紧到 20 日 +4.28%、摸 +15% 36.7%（但会漏 002990 型）。

优势是「跌得少、期望更高」，不是稳赚策略，仓位要控制。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .indicators import is_st, mean, moving_average, slope, stdev
from .patterns import StockContext

# ---------------------------------------------------------------- 特征字段

LOWPOS_FEATURE_KEYS: Tuple[str, ...] = (
    # 位置 / 暴跌
    "dd250", "dd120", "dd60", "pos250", "vs_hh250",
    "low_age60", "low_age120",
    # 均线粘合
    "spread_now", "spread_min5", "spread_min10", "spread_min20", "conv_days",
    # 均线
    "ma5", "ma10", "ma15", "ma20", "ma30",
    "ma5_slope", "ma20_slope", "bias5", "bias20", "ma10_ma20",
    # MACD
    "macd_dif", "macd_dea", "macd_hist", "hist_up_days", "hist_slope3",
    # 金叉
    "days_since_cross", "cross_spread",
    # 量能
    "vol_ratio", "vol_today", "vol_updown",
    # 动量 / 价格
    "chg1", "chg3", "ret20", "ret60", "price",
)


@dataclass
class LowPosConfig:
    """「低位均线粘合」判定阈值。

    默认值是 ``research/28_lowpos_study.py`` 在 2020-2026 全市场样本上标定的
    ``DEFAULT`` 档（能同时命中用户两个样本，每日约 10~20 只）。
    """

    # ---- 模式 ----
    # early = 低位粘合 + 刚金叉（买在启动前）
    # breakout = 低位粘合 + 已放量大阳/涨停突破（用户 2026-10 新样本）
    # both = 两种都扫（默认）
    mode: str = "both"

    # ---- 前期暴跌 ----
    min_drawdown: float = 0.30        # 250 日区间回撤下限
    max_position: float = 0.45        # 250 日区间位置上限（越低越好）
    max_close_vs_hh250: float = 0.75  # 收盘 / 250 日最高价 上限
    min_drawdown_120: float = 0.35    # 近 120 日回撤下限（最近一段暴跌）
    max_low_age: int = 45             # 近 60 日最低点距今天数上限（低点要新鲜）
    min_ret20: float = -0.30          # 近 20 日跌幅下限（防止还在崩）

    # ---- 均线粘合 ----
    conv_window: int = 10             # 观察最近 N 天的离散度
    max_spread: float = 0.025         # 近 conv_window 日「四线最小离散度」上限
    max_spread_now: float = 0.040     # 当前离散度上限（已拉开的不算）

    # ---- 拐头向上（early）----
    cross_days: int = 5               # 最近 N 日内出现 MA5 上穿 MA10
    max_cross_spread: float = 0.040   # 金叉当天的离散度上限
    min_bias20: float = -0.06         # 收盘相对 MA20 的偏离下限
    max_bias20: float = 0.08          # 偏离上限（已经涨飞的不追）
    require_hist_pos: bool = True     # 要求 MACD 柱 ≥ 0
    min_ma20_slope: float = -0.999    # MA20 3 日斜率下限（可选，默认关闭）

    # ---- 突破加速（breakout，对齐 2026-10 的 6 个新样本）----
    max_position_breakout: float = 0.65       # 突破时位置可以高一些
    max_close_vs_hh250_breakout: float = 0.87 # 但仍在 250 日高位下方
    min_spread20: float = 0.030               # 近 20 日最小离散度（平台粘合）
    max_spread_now_breakout: float = 0.060    # 突破当天离散度自然发散，放宽到 6%
    breakout_min_gain: float = 0.04           # 当日涨幅 ≥ 4%
    breakout_min_gain3: float = 0.08          # 或近 3 日涨幅 ≥ 8%
    breakout_min_vol: float = 0.70            # 今日量 / 前5日均量 ≥ 0.7
    breakout_max_bias20: float = 0.20         # BIAS20 ≤ 20%
    breakout_min_ma10_ma20: float = 0.990     # MA10 ≥ MA20×0.99
    breakout_min_hist: float = -0.005         # MACD 柱 ≥ -0.005

    # ---- 量能 / 基本 ----
    min_vol_trend: float = 0.0        # 量比(5日/20日) 下限；设 1.1 可收紧（推荐）
    min_price: float = 2.0
    exclude_st: bool = True
    min_bars: int = 260


# ---------------------------------------------------------------- 指标工具


def _argmin_age(values: Sequence[Optional[float]], window: int, d: int) -> Optional[int]:
    """第 d 根（含）往前 window 根中，最低点距今天数（0 = 就是今天）。"""
    lo = max(0, d - window + 1)
    best_i: Optional[int] = None
    best: Optional[float] = None
    for i in range(lo, d + 1):
        v = values[i]
        if v is None:
            continue
        if best is None or v < best:
            best = v
            best_i = i
    return (d - best_i) if best_i is not None else None


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ---------------------------------------------------------------- 特征提取


def extract_lowpos(ctx: StockContext, d: int, cfg: Optional[LowPosConfig] = None
                   ) -> Optional[Dict[str, Any]]:
    """提取第 ``d`` 根（含）当天的「低位粘合」特征。无未来函数。"""
    cfg = cfg or LowPosConfig()
    n = ctx.n
    if d < cfg.min_bars - 1 or d >= n:
        return None
    c = ctx.closes[d]
    if not c:
        return None

    # ---- 均线（含 MA15）----
    ma15 = moving_average(ctx.closes, 15)
    ma5, ma10, ma20, ma30 = ctx.ma5[d], ctx.ma10[d], ctx.ma20[d], ctx.ma30[d]
    m15 = ma15[d]
    if not (ma5 and ma10 and m15 and ma20):
        return None

    def _spread_at(k: int) -> Optional[float]:
        vals = [ctx.ma5[k], ctx.ma10[k], ma15[k], ctx.ma20[k]]
        vals = [v for v in vals if v]
        if len(vals) < 4:
            return None
        avg = sum(vals) / len(vals)
        return (max(vals) - min(vals)) / avg if avg else None

    spread_hist: List[Optional[float]] = []
    lookback = max(cfg.conv_window, 20)
    for k in range(max(0, d - lookback + 1), d + 1):
        spread_hist.append(_spread_at(k))
    valid = [s for s in spread_hist if s is not None]
    if not valid:
        return None
    spread_now = valid[-1]
    spread_min5 = min(valid[-5:]) if len(valid) >= 5 else min(valid)
    spread_min10 = min(valid[-10:]) if len(valid) >= 10 else min(valid)
    spread_min20 = min(valid[-20:]) if len(valid) >= 20 else min(valid)
    conv_days = sum(1 for s in valid[-cfg.conv_window:] if s <= cfg.max_spread)

    # ---- 位置 / 前期暴跌 ----
    hh250 = None
    ll250 = None
    for k in range(max(0, d - 249), d + 1):
        hv, lv = ctx.highs[k], ctx.lows[k]
        if hv is not None:
            hh250 = hv if hh250 is None else max(hh250, hv)
        if lv is not None:
            ll250 = lv if ll250 is None else min(ll250, lv)
    hh120 = ctx.hh120[d]
    ll120 = None
    for k in range(max(0, d - 119), d + 1):
        lv = ctx.lows[k]
        if lv is not None:
            ll120 = lv if ll120 is None else min(ll120, lv)
    hh60 = ctx.hh60[d]
    ll60 = None
    for k in range(max(0, d - 59), d + 1):
        lv = ctx.lows[k]
        if lv is not None:
            ll60 = lv if ll60 is None else min(ll60, lv)
    if not hh250 or not ll250 or not hh120 or not ll120 or not hh60:
        return None

    dd250 = (hh250 - ll250) / hh250
    dd120 = (hh120 - ll120) / hh120
    dd60 = (hh60 - ll60) / hh60 if ll60 else 0.0
    pos250 = (c - ll250) / (hh250 - ll250) if hh250 > ll250 else 1.0
    vs_hh250 = c / hh250

    low_age60 = _argmin_age(ctx.lows, 60, d)
    low_age120 = _argmin_age(ctx.lows, 120, d)
    if low_age60 is None:
        return None

    # ---- 金叉（MA5 上穿 MA10）----
    days_since_cross: Optional[int] = None
    cross_spread: Optional[float] = None
    for k in range(d, max(0, d - 60), -1):
        a0, b0 = ctx.ma5[k - 1], ctx.ma10[k - 1]
        a1, b1 = ctx.ma5[k], ctx.ma10[k]
        if None in (a0, b0, a1, b1):
            continue
        if a0 <= b0 and a1 > b1:
            days_since_cross = d - k
            cross_spread = _spread_at(k)
            break

    # ---- MACD ----
    dif, dea, hist = ctx.dif[d], ctx.dea[d], ctx.hist[d]
    hist_up_days = 0
    for k in range(d, max(0, d - 20), -1):
        h1, h0 = ctx.hist[k], ctx.hist[k - 1]
        if h1 is None or h0 is None:
            break
        if h1 > h0:
            hist_up_days += 1
        else:
            break

    # ---- 量能 ----
    vol_now = mean([v for v in ctx.vols[max(0, d - 4): d + 1]])
    vol_base = mean([v for v in ctx.vols[max(0, d - 19): d + 1]])
    vol_ratio = (vol_now / vol_base) if vol_base else 0.0
    up_v, dn_v = [], []
    p0 = max(1, d - 59)
    for k in range(p0, d + 1):
        pc, cc = ctx.closes[k - 1], ctx.closes[k]
        if not pc or cc is None:
            continue
        (up_v if cc > pc else dn_v).append(ctx.vols[k])
    vol_updown = (mean(up_v) / mean(dn_v)) if (up_v and dn_v and mean(dn_v)) else 1.0

    # ---- 动量 ----
    ret20 = (c / ctx.closes[d - 20] - 1) if (d >= 20 and ctx.closes[d - 20]) else 0.0
    ret60 = (c / ctx.closes[d - 60] - 1) if (d >= 60 and ctx.closes[d - 60]) else 0.0
    bias5 = (c - ma5) / ma5 if ma5 else 0.0
    bias20 = (c - ma20) / ma20 if ma20 else 0.0

    # ---- 动量 / 突破 ----
    chg1 = (c / ctx.closes[d - 1] - 1.0) * 100 if (d >= 1 and ctx.closes[d - 1]) else 0.0
    chg3 = (c / ctx.closes[d - 3] - 1.0) * 100 if (d >= 3 and ctx.closes[d - 3]) else 0.0
    vol_today = 0.0
    if d >= 5:
        base5 = mean([v for v in ctx.vols[d - 5:d]])
        vol_today = (ctx.vols[d] / base5) if base5 else 0.0
    hist_slope3 = 0.0
    if d >= 3 and hist is not None and ctx.hist[d - 3] is not None:
        hist_slope3 = hist - ctx.hist[d - 3]
    ma10_ma20 = (ma10 / ma20 - 1.0) if ma20 else 0.0

    return {
        "dd250": dd250, "dd120": dd120, "dd60": dd60,
        "pos250": pos250, "vs_hh250": vs_hh250,
        "low_age60": float(low_age60), "low_age120": float(low_age120 or 0),
        "spread_now": spread_now, "spread_min5": spread_min5,
        "spread_min10": spread_min10, "spread_min20": spread_min20,
        "conv_days": float(conv_days),
        "ma5": ma5, "ma10": ma10, "ma15": m15, "ma20": ma20, "ma30": ma30 or 0.0,
        "ma5_slope": slope(ctx.ma5[: d + 1], 3) or 0.0,
        "ma20_slope": slope(ctx.ma20[: d + 1], 3) or 0.0,
        "bias5": bias5 * 100.0, "bias20": bias20 * 100.0,
        "ma10_ma20": ma10_ma20 * 100.0,
        "macd_dif": dif or 0.0, "macd_dea": dea or 0.0,
        "macd_hist": hist or 0.0, "hist_up_days": float(hist_up_days),
        "hist_slope3": hist_slope3,
        "days_since_cross": float(days_since_cross if days_since_cross is not None else 999),
        "cross_spread": cross_spread if cross_spread is not None else 9.0,
        "vol_ratio": vol_ratio, "vol_today": vol_today, "vol_updown": vol_updown,
        "chg1": chg1, "chg3": chg3,
        "ret20": ret20 * 100.0, "ret60": ret60 * 100.0,
        "price": c,
    }


# ---------------------------------------------------------------- 实盘判定


def quick_lowpos_ok(bars: Sequence[Dict[str, Any]], cfg: LowPosConfig) -> bool:
    """廉价预筛（不算 MA/MACD）：只做「暴跌 + 低位」的粗筛。

    比正式判定放宽 10~20%，确保不漏，同时砍掉大部分明显不符合的股票。
    ``mode=both/breakout`` 时用突破档的更宽松位置上限。
    """
    if len(bars) < cfg.min_bars:
        return False
    c = bars[-1].get("close")
    if not c or c < cfg.min_price:
        return False
    seg = bars[-250:]
    hs = [b.get("high") for b in seg if b.get("high")]
    ls = [b.get("low") for b in seg if b.get("low")]
    if not hs or not ls:
        return False
    hh, ll = max(hs), min(ls)
    if not hh or hh <= ll:
        return False
    if cfg.mode == "early":
        max_pos, max_vs, check_age = cfg.max_position, cfg.max_close_vs_hh250, True
    else:
        max_pos = cfg.max_position_breakout
        max_vs = cfg.max_close_vs_hh250_breakout
        check_age = False
    if (hh - ll) / hh < cfg.min_drawdown - 0.05:
        return False
    if c / hh > max_vs + 0.05:
        return False
    if (c - ll) / (hh - ll) > max_pos + 0.10:
        return False
    # 低点不能太老（只对 early 档；突破档的底可以是 2~3 个月前的）
    seg60 = [b.get("low") for b in bars[-60:] if b.get("low")]
    if check_age and seg60:
        age = seg60[::-1].index(min(seg60))
        if age > cfg.max_low_age + 15:
            return False
    return True


def detect_low_position(
    bars: Sequence[Dict[str, Any]],
    code: str,
    name: str = "",
    cfg: Optional[LowPosConfig] = None,
    quote: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """判定当前是否处于「低位均线粘合 + 拐头向上」状态。

    返回 ``None`` 表示不符合（常态）。
    """
    cfg = cfg or LowPosConfig()
    if cfg.exclude_st and is_st(name):
        return None
    if not quick_lowpos_ok(bars, cfg):
        return None

    keep = min(len(bars), max(cfg.min_bars + 60, 340))
    ctx = StockContext(list(bars[-keep:]), code, name)
    d = ctx.n - 1
    f = extract_lowpos(ctx, d, cfg)
    if f is None:
        return None

    # ---- 早期拐头（early）----
    early_ok = (
        f["dd250"] >= cfg.min_drawdown
        and f["pos250"] <= cfg.max_position
        and f["vs_hh250"] <= cfg.max_close_vs_hh250
        and f["dd120"] >= cfg.min_drawdown_120
        and f["low_age60"] <= cfg.max_low_age
        and f["ret20"] >= cfg.min_ret20 * 100.0
        and f["spread_min10"] <= cfg.max_spread
        and f["spread_now"] <= cfg.max_spread_now
        and (f["days_since_cross"] <= cfg.cross_days if cfg.cross_days > 0 else f["ma5"] > f["ma10"])
        and (f["cross_spread"] <= cfg.max_cross_spread if cfg.cross_days > 0 else True)
        and f["price"] >= f["ma5"] * 0.99
        and (not cfg.require_hist_pos or f["macd_hist"] >= 0)
        and (cfg.min_bias20 * 100 <= f["bias20"] <= cfg.max_bias20 * 100)
        and (cfg.min_ma20_slope <= -0.99 or f["ma20_slope"] >= cfg.min_ma20_slope)
        and (not cfg.min_vol_trend or f["vol_ratio"] >= cfg.min_vol_trend)
    )

    # ---- 突破加速（breakout，对齐用户 2026-10 的 6 个新样本）----
    breakout_ok = (
        f["dd250"] >= cfg.min_drawdown
        and f["pos250"] <= cfg.max_position_breakout
        and f["vs_hh250"] <= cfg.max_close_vs_hh250_breakout
        and f["spread_min20"] <= cfg.min_spread20
        and f["spread_now"] <= cfg.max_spread_now_breakout
        and (f["chg1"] >= cfg.breakout_min_gain * 100.0
             or f["chg3"] >= cfg.breakout_min_gain3 * 100.0)
        and (f["vol_today"] >= cfg.breakout_min_vol or f["chg1"] >= 9.5)
        and f["ma5"] > f["ma10"]
        and f["ma10"] >= f["ma20"] * cfg.breakout_min_ma10_ma20
        and f["price"] >= f["ma5"] * 0.99
        and f["macd_hist"] >= cfg.breakout_min_hist
        and f["bias20"] <= cfg.breakout_max_bias20 * 100.0
    )

    if cfg.mode == "early":
        ok = early_ok
    elif cfg.mode == "breakout":
        ok = breakout_ok
    else:
        ok = early_ok or breakout_ok
    if not ok:
        return None
    is_breakout = bool(breakout_ok and cfg.mode != "early")

    # ---- 打分（0~100，仅用于排序）----
    # 拐头档排序：低位置 + 近 20 日偏弱 + 低价格（2021-2026 组合回测里唯一为正的排序）
    early_score = 100.0 * (
        0.60 * _clip01((0.65 - f["pos250"]) / 0.65)
        + 0.30 * _clip01(-f["ret20"] / 20.0)
        + 0.10 * _clip01((30.0 - f["price"]) / 30.0)
    )
    breakout_score = 100.0 * (
        0.25 * _clip01((cfg.min_spread20 - f["spread_min20"]) / max(cfg.min_spread20, 1e-9))
        + 0.20 * _clip01((cfg.max_position_breakout - f["pos250"]) / max(cfg.max_position_breakout, 1e-9))
        + 0.15 * _clip01(f["dd250"] / 0.70)
        + 0.20 * min(1.2, _clip01(f["chg1"] / 10.0))
        + 0.10 * _clip01(f["vol_today"] / 3.0)
        + 0.10 * _clip01(f["hist_slope3"] / 0.20)
    )
    if cfg.mode == "early":
        score = early_score
    elif cfg.mode == "breakout":
        score = breakout_score
    elif early_ok and breakout_ok:
        score = max(early_score, breakout_score)
    elif early_ok:
        score = early_score
    else:
        score = breakout_score

    rec: Dict[str, Any] = {
        "strategy": "low_position",
        "code": code,
        "name": name,
        "score": round(score, 1),
        "模式": "突破" if is_breakout else "拐头",
        "决策日": bars[-1].get("date"),
        "现价": round(f["price"], 2),
        "今日涨幅%": round(f["chg1"], 2),
        "3日涨幅%": round(f["chg3"], 2),
        "今日量比": round(f["vol_today"], 2),
        # 位置
        "250日位置%": round(f["pos250"] * 100, 0),
        "距250日高%": round((f["vs_hh250"] - 1) * 100, 1),
        "250日回撤%": round(f["dd250"] * 100, 0),
        "120日回撤%": round(f["dd120"] * 100, 0),
        "低点天数": int(f["low_age60"]),
        # 粘合
        "均线离散度%": round(f["spread_min10"] * 100, 2),
        "20日离散度%": round(f["spread_min20"] * 100, 2),
        "当前离散度%": round(f["spread_now"] * 100, 2),
        "粘合天数": int(f["conv_days"]),
        # 均线 / 拐头
        "MA5": round(f["ma5"], 2),
        "MA10": round(f["ma10"], 2),
        "MA15": round(f["ma15"], 2),
        "MA20": round(f["ma20"], 2),
        "金叉后天数": int(f["days_since_cross"]),
        "MA5>MA10": bool(f["ma5"] > f["ma10"]),
        "MA5斜率%": round(f["ma5_slope"] * 100, 2),
        "MA20斜率%": round(f["ma20_slope"] * 100, 2),
        "BIAS5%": round(f["bias5"], 2),
        "BIAS20%": round(f["bias20"], 2),
        # MACD
        "MACD_DIF": round(f["macd_dif"], 3),
        "MACD_DEA": round(f["macd_dea"], 3),
        "MACD柱": round(f["macd_hist"], 3),
        "红柱放大天数": int(f["hist_up_days"]),
        # 量能 / 动量
        "量比(5/20)": round(f["vol_ratio"], 2),
        "涨跌量比": round(f["vol_updown"], 2),
        "20日涨幅%": round(f["ret20"], 1),
        "60日涨幅%": round(f["ret60"], 1),
    }
    if quote:
        rec.update({
            "现价": quote.get("price") or rec["现价"],
            "今日涨跌%": quote.get("change_pct"),
            "成交额": quote.get("amount"),
            "换手率%": quote.get("turnover_rate"),
            "总市值": quote.get("total_mv"),
            "行业": quote.get("industry"),
        })
        # 用实时市值/成交额把「得分」重排成优化版排序：
        # 低位置 + 近20日弱 + 低价格 + 小市值 + 低成交额（与 30 万回测口径一致）
        if not is_breakout and cfg.mode != "breakout":
            mcap = (quote.get("total_mv") or 0) / 1e8
            amt = (quote.get("amount") or 0) / 1e8
            if mcap > 0:
                raw = (-0.6 * f["pos250"] - 0.3 * (f["ret20"] / 100.0)
                       - 0.1 * f["price"] / 30.0
                       - 0.15 * math.log1p(mcap)
                       - 0.05 * math.log1p(max(amt, 0.01)))
                rec["score"] = round(max(0.0, min(100.0, 100.0 * (raw + 1.9) / 2.04)), 1)
    return rec
