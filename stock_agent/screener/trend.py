"""趋势票（涨不停不涨停 / 慢牛）—— 特征提取、样本挖掘与判定。

形态定义
--------
与「二次涨停」不同，这里找的是**持续稳步上涨**的票：

::

    长期横盘 → 均线开始多头 → 连续 15~30 天小步上涨（每天 0.5%~5%，从不涨停）
             → 回调不破 MA10/MA20 → MACD 在零轴上方红柱持续放大
             → 最后进入加速（连续涨停）

典型样本（用户提供）：

* ``002990 盛视科技``  2026-03-13(26.22) → 2026-04-13(34.70)，20 个交易日 +32%，
  其间无涨停，MA5>MA10>MA20>MA30 全程多头，MACD 于 03-20 上穿零轴后红柱持续放大，
  04-14 / 04-15 连续涨停加速。
* ``002655 共达电声``  2026-03-24(16.38) → 2026-05-07(35.78)，30 个交易日 +118%，
  其间仅 04-29 一根涨停，其余都是 1%~7% 的小阳线。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .patterns import StockContext
from .indicators import (
    board_limit_ratio,
    is_limit_up_bar,
    is_st,
    mean,
    moving_average,
    slope,
    stdev,
)

# ---------------------------------------------------------------- 特征字段

TREND_FEATURE_KEYS: Tuple[str, ...] = (
    # 结构 / 节奏
    "win",                # 观察窗口（交易日）
    "up_ratio",           # 上涨天数占比
    "total_gain",         # 窗口累计涨幅
    "max_gain",           # 单日最大涨幅
    "max_loss",           # 单日最大跌幅（负数）
    "max_pullback",       # 窗口内最大回撤（从滚动最高点）
    "ret_mean",           # 日均涨幅
    "ret_std",            # 日涨幅标准差（越小越平滑）
    "up_streak",          # 当前连阳天数
    "above_ma5_ratio",    # 收盘价在 MA5 之上的天数占比
    "above_ma10_ratio",
    "new_high_ratio",     # 创 20 日新高的天数占比
    # 均线
    "ma_bull",            # MA5>MA10>MA20>MA30
    "ma_bull_ratio",      # 窗口内完美多头的天数占比
    "ma_spread",          # (MA5-MA30)/MA30 均线发散度
    "ma5_slope", "ma10_slope", "ma20_slope", "ma30_slope",
    "bias5", "bias10", "bias20",   # 乖离率
    # MACD
    "dif_n", "dea_n", "hist_n",    # 按价格归一化
    "hist_slope",                  # MACD 柱近 3 日变化率
    "hist_pos_ratio",              # 红柱天数占比
    "dif_above_zero_days",         # DIF > 0 的连续天数
    "macd_days_since_cross",       # 距最近一次金叉的天数
    # 量能
    "vol_ratio",          # 窗口均量 / 前窗口均量
    "vol_trend",          # 后 5 日 / 前 5 日均量
    "vol_stability",      # 量能标准差 / 均值（越小越稳）
    "vol_updown",         # 涨日均量 / 跌日均量
    # 位置
    "ret_60d", "ret_120d",
    "close_vs_hh60", "close_vs_hh120",
    "price",
)

TREND_META_KEYS: Tuple[str, ...] = (
    "code", "name", "date",
    "fwd_ret_5d", "fwd_ret_10d", "fwd_ret_20d",
    "fwd_max_20d", "fwd_min_20d", "fwd_limit_up_20d", "fwd_limit_up_days",
)


@dataclass
class MineTrendConfig:
    """样本挖掘的宽松条件（用于构造研究样本集）。"""

    window: int = 20
    min_up_ratio: float = 0.55
    max_single_gain: float = 10.5   # 容许区间内出现涨停（长趋势中间可以有涨停）（用户样本都有）
    max_single_loss: float = -6.0
    min_total_gain: float = 3.0
    max_total_gain: float = 200.0
    min_history: int = 180
    horizon: int = 20


@dataclass
class SteadyTrendConfig:
    """趋势票判定阈值。

    核心思路：**不靠手工阈值，而是靠“与用户两个样本的相似度”**。
    先用宽松形态条件构造样本空间（必须与挖掘时一致），再算 14 维标准化欧氏距离，
    距离越小越像模板。标定表见 :data:`TREND_DIST_CALIBRATION`。
    """

    # --- 样本空间前置条件（必须与 MineTrendConfig 一致）---
    window: int = 20
    min_up_ratio: float = 0.55
    max_single_gain: float = 10.5   # 容许区间内出现涨停（长趋势中间可以有涨停）
    max_single_loss: float = -6.0
    min_total_gain: float = 3.0
    max_total_gain: float = 200.0

    # --- 核心：相似度阈值 ---
    max_distance: float = 1.90      # ≈ 每日 2~3 只，摸+15% 概率 42.9%（基准 28.6%）

    # --- 安全护栏（避免离谱匹配）---
    guard_ma_bull: bool = False     # 不强制（模板本身是1，但交给距离判断更好）
    max_pullback: float = 0.12
    min_close_vs_hh60: float = 0.85

    exclude_st: bool = True
    min_bars: int = 120


# ============================================================================
# 模板：用户提供的两只票「加速前一天」（002990 盛视科技 / 002655 共达电声）
# 参数由 research/14_build_template.py 在 25 万样本上标定
# ============================================================================

TREND_TEMPLATE_KEYS: Tuple[str, ...] = (
    "up_ratio", "max_pullback", "ret_std", "ma_bull", "ma_bull_ratio",
    "ma_spread", "bias5", "bias10", "hist_n", "hist_slope",
    "hist_pos_ratio", "vol_updown", "vol_trend", "close_vs_hh60",
)

TREND_TEMPLATE_MEAN: Dict[str, float] = {
    "up_ratio": 0.604456, "max_pullback": 0.0559054, "ret_std": 2.35801,
    "ma_bull": 0.624619, "ma_bull_ratio": 0.376791, "ma_spread": 0.0651859,
    "bias5": 1.62838, "bias10": 3.32651, "hist_n": 1.23646,
    "hist_slope": 0.79772, "hist_pos_ratio": 0.759217, "vol_updown": 1.24303,
    "vol_trend": 1.72406, "close_vs_hh60": 0.914978,
}

TREND_TEMPLATE_STD: Dict[str, float] = {
    "up_ratio": 0.0570962, "max_pullback": 0.0280252, "ret_std": 0.873764,
    "ma_bull": 0.484221, "ma_bull_ratio": 0.301644, "ma_spread": 0.0583616,
    "bias5": 3.25413, "bias10": 4.97219, "hist_n": 1.47505,
    "hist_slope": 246.676, "hist_pos_ratio": 0.219976, "vol_updown": 0.375903,
    "vol_trend": 3.8265, "close_vs_hh60": 0.0726992,
}

TREND_TEMPLATE_VECTORS: Tuple[Tuple[float, ...], ...] = (
    # 002990 盛视科技 @2026-04-13（其后 5 日 +40.9%）  —— 已按 ±3 截断
    (1.6734, -0.8530, 0.5974, 0.7752, 0.2427, 1.4190, 0.9832, 1.3355,
     1.3251, -0.0014, 1.0946, 0.5559, 0.0031, 1.0182),
    # 002655 共达电声 @2026-05-06（其后 5 日 +12.7%）  —— 已按 ±3 截断
    (3.0000, -1.3882, 0.4898, 0.7752, 2.0660, 3.0000, 2.4334, 3.0000,
     2.7386, 0.0007, 1.0946, -0.1065, -0.1498, 0.8669),
    # 600802 福建水泥 @2026-09-04（趋势中段 +14%，含 1 次涨停）
    (-0.9538, -0.9137, 0.5540, 0.7752, 1.7345, 0.2059, 1.5106, 1.0853,
     0.0858, -0.0017, 1.0946, -0.0969, 0.0109, 1.1695),
    # 600802 福建水泥 @2026-09-16（趋势后段 +31%，含 2 次涨停、量能放大 8.6 倍）
    (-0.0780, -0.2143, 1.5423, 0.7752, 2.0660, 1.7702, 1.1339, 1.5121,
     1.2410, -0.0011, 1.0946, 0.0146, 1.8030, 0.5588),
)

#: ``(距离上限, 未来20日摸到+15%的概率, 20日收益, 20日最大回撤)``
#: 口径：**仅深交所**（两个模板本身都是深交所股票），2025-01 ~ 2026-09 时间外样本
TREND_DIST_CALIBRATION: Tuple[Tuple[float, float, float, float], ...] = (
    (1.50, 0.4182, 0.02382, -0.10011),
    (1.70, 0.4000, 0.02616, -0.09947),
    (1.90, 0.3673, 0.01666, -0.10250),
    (2.10, 0.3603, 0.01982, -0.10063),
    (2.30, 0.3551, 0.01517, -0.10196),
    (2.50, 0.3547, 0.01645, -0.10022),
    (2.80, 0.3423, 0.01666, -0.09871),
    (3.20, 0.3276, 0.01704, -0.09518),
    (3.80, 0.3003, 0.01577, -0.08912),
    (99.0, 0.2676, 0.01715, -0.07896),
)

#: 深交所口径的基准（全部趋势样本）
TREND_BASELINE = {
    "hit15": 0.2676, "ret20": 0.01715, "min20": -0.07896, "ret5": 0.0044,
}

_Z_CLIP = 3.0


def trend_distance(feats: Dict[str, Any]) -> float:
    """计算特征向量到模板集合的最小标准化欧氏距离（z 值截断在 ±3）。"""
    z = []
    for k in TREND_TEMPLATE_KEYS:
        v = feats.get(k)
        if v is None:
            z.append(0.0)
            continue
        s = TREND_TEMPLATE_STD.get(k) or 1.0
        zi = (float(v) - TREND_TEMPLATE_MEAN.get(k, 0.0)) / s
        z.append(max(-_Z_CLIP, min(_Z_CLIP, zi)))
    best = float("inf")
    for tpl in TREND_TEMPLATE_VECTORS:
        d = 0.0
        for a, b in zip(z, tpl):
            d += (a - b) ** 2
        best = min(best, d ** 0.5)
    return best


def distance_calibration(dist: float) -> Tuple[float, float, float]:
    """距离 → ``(摸到+15%概率, 20日预期收益, 20日预期最大回撤)``。"""
    for thr, hit, ret20, mn in TREND_DIST_CALIBRATION:
        if dist <= thr:
            return hit, ret20, mn
    last = TREND_DIST_CALIBRATION[-1]
    return last[1], last[2], last[3]


# ---------------------------------------------------------------- 特征提取


def extract_trend(ctx: StockContext, d: int, window: int) -> Optional[Dict[str, Any]]:
    """提取第 ``d`` 根（含）往前 ``window`` 根的「稳步上涨」特征。无未来函数。"""
    if d < ctx.n - 1:
        pass
    start = d - window + 1
    if start < 1 or d >= ctx.n:
        return None
    if d < 60:
        return None

    seg = range(start, d + 1)
    closes = [ctx.closes[k] for k in seg]
    if any(c is None for c in closes) or not closes[0]:
        return None

    rets: List[float] = []
    for k in seg:
        pc = ctx.closes[k - 1]
        if not pc or ctx.closes[k] is None:
            return None
        rets.append((ctx.closes[k] - pc) / pc * 100.0)

    total_gain = (closes[-1] / closes[0] - 1.0) * 100.0
    up_ratio = sum(1 for r in rets if r > 0) / len(rets)

    # 窗口内最大回撤（从滚动最高收盘算）
    peak = closes[0]
    max_pb = 0.0
    for c in closes:
        peak = max(peak, c)
        max_pb = max(max_pb, (peak - c) / peak)

    # 连阳
    streak = 0
    for k in range(d, start - 1, -1):
        if ctx.closes[k] is not None and ctx.closes[k - 1] and ctx.closes[k] > ctx.closes[k - 1]:
            streak += 1
        else:
            break

    above5 = sum(1 for k in seg if ctx.ma5[k] and ctx.closes[k] >= ctx.ma5[k]) / len(rets)
    above10 = sum(1 for k in seg if ctx.ma10[k] and ctx.closes[k] >= ctx.ma10[k]) / len(rets)

    new_high = 0
    for k in seg:
        hh = ctx.hh20[k]
        if ctx.highs[k] is not None and hh is not None and ctx.highs[k] >= hh - 1e-9:
            new_high += 1
    new_high_ratio = new_high / len(rets)

    # 均线多头
    bull_days = 0
    for k in seg:
        a, b, c_, e = ctx.ma5[k], ctx.ma10[k], ctx.ma20[k], ctx.ma30[k]
        if a and b and c_ and e and a > b > c_ > e:
            bull_days += 1
    ma_bull_ratio = bull_days / len(rets)

    m5, m10, m20, m30 = ctx.ma5[d], ctx.ma10[d], ctx.ma20[d], ctx.ma30[d]
    ma_bull = bool(m5 and m10 and m20 and m30 and m5 > m10 > m20 > m30)
    ma_spread = ((m5 - m30) / m30) if (m5 and m30) else 0.0

    close_d = ctx.closes[d]

    # MACD
    dif, dea, hist = ctx.dif[d], ctx.dea[d], ctx.hist[d]
    hist_win = [ctx.hist[k] for k in seg if ctx.hist[k] is not None]
    hist_pos_ratio = (
        sum(1 for h in hist_win if h > 0) / len(hist_win) if hist_win else 0.0
    )
    dif_pos_days = 0
    for k in range(d, max(0, d - window), -1):
        if ctx.dif[k] is not None and ctx.dif[k] > 0:
            dif_pos_days += 1
        else:
            break
    cross_ago = 999
    for k in range(d, max(1, d - 120), -1):
        a0, b0 = ctx.dif[k - 1], ctx.dea[k - 1]
        a1, b1 = ctx.dif[k], ctx.dea[k]
        if None in (a0, b0, a1, b1):
            continue
        if a0 <= b0 and a1 > b1:
            cross_ago = d - k
            break

    # 量能
    vols = [ctx.vols[k] for k in seg]
    prior = [ctx.vols[k] for k in range(max(0, start - window), start)] or [mean(vols)]
    vol_ratio = (mean(vols) / mean(prior)) if mean(prior) else 1.0
    last5 = mean(vols[-5:])
    first5 = mean(vols[:5])
    vol_trend = (last5 / first5) if first5 else 1.0
    vol_stability = (stdev(vols) / mean(vols)) if mean(vols) else 0.0
    up_v = [ctx.vols[k] for k, r in zip(seg, rets) if r > 0]
    dn_v = [ctx.vols[k] for k, r in zip(seg, rets) if r < 0]
    vol_updown = (mean(up_v) / mean(dn_v)) if (up_v and dn_v and mean(dn_v)) else 1.0

    # 位置
    hh60 = ctx.hh60[d] or close_d
    hh120 = ctx.hh120[d] or close_d

    return {
        "win": window,
        "up_ratio": up_ratio,
        "total_gain": total_gain,
        "max_gain": max(rets),
        "max_loss": min(rets),
        "max_pullback": max_pb,
        "ret_mean": mean(rets),
        "ret_std": stdev(rets),
        "up_streak": streak,
        "above_ma5_ratio": above5,
        "above_ma10_ratio": above10,
        "new_high_ratio": new_high_ratio,
        "ma_bull": int(ma_bull),
        "ma_bull_ratio": ma_bull_ratio,
        "ma_spread": ma_spread,
        "ma5_slope": slope(ctx.ma5[: d + 1], 3) or 0.0,
        "ma10_slope": slope(ctx.ma10[: d + 1], 3) or 0.0,
        "ma20_slope": slope(ctx.ma20[: d + 1], 3) or 0.0,
        "ma30_slope": slope(ctx.ma30[: d + 1], 3) or 0.0,
        "bias5": ((close_d - m5) / m5 * 100) if m5 else 0.0,
        "bias10": ((close_d - m10) / m10 * 100) if m10 else 0.0,
        "bias20": ((close_d - m20) / m20 * 100) if m20 else 0.0,
        "dif_n": (dif / close_d * 100) if (dif is not None and close_d) else 0.0,
        "dea_n": (dea / close_d * 100) if (dea is not None and close_d) else 0.0,
        "hist_n": (hist / close_d * 100) if (hist is not None and close_d) else 0.0,
        "hist_slope": slope(ctx.hist[: d + 1], 3) or 0.0,
        "hist_pos_ratio": hist_pos_ratio,
        "dif_above_zero_days": dif_pos_days,
        "macd_days_since_cross": cross_ago,
        "vol_ratio": vol_ratio,
        "vol_trend": vol_trend,
        "vol_stability": vol_stability,
        "vol_updown": vol_updown,
        "ret_60d": ((close_d / ctx.closes[d - 60] - 1) * 100) if ctx.closes[d - 60] else 0.0,
        "ret_120d": ((close_d / ctx.closes[d - 120] - 1) * 100) if ctx.closes[d - 120] else 0.0,
        "close_vs_hh60": close_d / hh60 if hh60 else 1.0,
        "close_vs_hh120": close_d / hh120 if hh120 else 1.0,
        "price": close_d,
    }


# ---------------------------------------------------------------- 样本挖掘


def mine_trend(
    bars: Sequence[Dict[str, Any]], code: str, name: str, cfg: Optional[MineTrendConfig] = None
) -> List[Dict[str, Any]]:
    """挖掘单只股票所有「稳步上涨状态」样本，带未来收益标签。"""
    cfg = cfg or MineTrendConfig()
    ctx = StockContext(bars, code, name)
    n = ctx.n
    if n < cfg.min_history + cfg.horizon + 5:
        return []

    out: List[Dict[str, Any]] = []
    for d in range(max(cfg.min_history, cfg.window), n - cfg.horizon):
        feats = extract_trend(ctx, d, cfg.window)
        if feats is None:
            continue
        # 宽松结构预筛，避免把明显不符合的也写进样本
        if feats["up_ratio"] < cfg.min_up_ratio:
            continue
        if feats["max_gain"] > cfg.max_single_gain:
            continue
        if feats["max_loss"] < cfg.max_single_loss:
            continue
        if not (cfg.min_total_gain <= feats["total_gain"] <= cfg.max_total_gain):
            continue

        c0 = ctx.closes[d]
        fwd: Dict[str, Any] = {}
        for h, key in ((5, "fwd_ret_5d"), (10, "fwd_ret_10d"), (20, "fwd_ret_20d")):
            c = ctx.closes[d + h] if d + h < n else None
            fwd[key] = ((c / c0 - 1) * 100) if (c0 and c) else None
        hi = [ctx.highs[k] for k in range(d + 1, min(d + 21, n)) if ctx.highs[k] is not None]
        lo = [ctx.lows[k] for k in range(d + 1, min(d + 21, n)) if ctx.lows[k] is not None]
        fwd["fwd_max_20d"] = ((max(hi) / c0 - 1) * 100) if (c0 and hi) else None
        fwd["fwd_min_20d"] = ((min(lo) / c0 - 1) * 100) if (c0 and lo) else None
        lu_days = [k - d for k in range(d + 1, min(d + 21, n)) if ctx.lu[k]]
        fwd["fwd_limit_up_20d"] = int(bool(lu_days))
        fwd["fwd_limit_up_days"] = lu_days[0] if lu_days else -1

        rec = {
            "code": code,
            "name": name,
            "date": bars[d].get("date"),
            **fwd,
            **feats,
        }
        out.append(rec)
    return out


def count_trend_samples(samples: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "samples": len(samples),
        "codes": len({s["code"] for s in samples}),
        "dates": len({s["date"] for s in samples}),
    }


# ---------------------------------------------------------------- 实盘判定


def sample_space_ok(feats: Dict[str, Any], cfg: SteadyTrendConfig) -> bool:
    """样本空间前置条件。

    **必须与研究阶段的 ``MineTrendConfig`` 保持一致**，否则特征分布不同，
    标准化距离的尺度就对不上，标定表会失效。
    """
    if feats.get("up_ratio") is None or feats["up_ratio"] < cfg.min_up_ratio:
        return False
    if feats["max_gain"] > cfg.max_single_gain:
        return False
    if feats["max_loss"] < cfg.max_single_loss:
        return False
    if not (cfg.min_total_gain <= feats["total_gain"] <= cfg.max_total_gain):
        return False
    return True


def quick_trend_ok(
    bars: Sequence[Dict[str, Any]], cfg: SteadyTrendConfig
) -> bool:
    """廉价预筛（不算 MA/MACD）。全市场扫描时先过这一层，能省 70%+ 的指标计算。"""
    w = cfg.window
    if len(bars) < w + 21:
        return False
    closes = [b.get("close") for b in bars[-(w + 1) :]]
    if any(c is None for c in closes):
        return False
    rets: List[float] = []
    for i in range(1, len(closes)):
        if not closes[i - 1]:
            return False
        rets.append((closes[i] - closes[i - 1]) / closes[i - 1] * 100.0)
    ups = sum(1 for r in rets if r > 0) / len(rets)
    if ups < cfg.min_up_ratio:
        return False
    if max(rets) > cfg.max_single_gain or min(rets) < cfg.max_single_loss:
        return False
    total = (closes[-1] / closes[0] - 1.0) * 100.0
    return cfg.min_total_gain <= total <= cfg.max_total_gain


def detect_steady_trend(
    bars: Sequence[Dict[str, Any]],
    code: str,
    name: str = "",
    cfg: Optional[SteadyTrendConfig] = None,
    quote: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """判定当前是否处于「像用户样本那样的趋势状态」。

    返回 ``None`` 表示不符合（这是常态——这种票很少）。
    """
    cfg = cfg or SteadyTrendConfig()
    if cfg.exclude_st and is_st(name):
        return None
    if len(bars) < cfg.min_bars:
        return None
    if not quick_trend_ok(bars, cfg):
        return None

    keep = min(len(bars), max(cfg.window + 140, 200))
    sub = list(bars[-keep:])
    ctx = StockContext(sub, code, name)
    d = ctx.n - 1
    feats = extract_trend(ctx, d, cfg.window)
    if feats is None:
        return None

    if not sample_space_ok(feats, cfg):
        return None

    # 安全护栏：避免距离算出来很近但形状很离谱
    if feats["max_pullback"] > cfg.max_pullback:
        return None
    if feats["close_vs_hh60"] < cfg.min_close_vs_hh60:
        return None
    if cfg.guard_ma_bull and not feats["ma_bull"]:
        return None

    dist = trend_distance(feats)
    if dist > cfg.max_distance:
        return None

    hit15, ret20, min20 = distance_calibration(dist)
    score = round(max(0.0, min(1.0, 1.0 - dist / max(cfg.max_distance * 1.8, 1e-6))) * 100, 1)

    rec: Dict[str, Any] = {
        "strategy": "steady_trend",
        "code": code,
        "name": name,
        "score": score,
        "distance": round(dist, 3),
        "相似度": round(max(0.0, 100.0 * (1.0 - dist / 4.0)), 1),
        "摸15概率": hit15,
        "预期20日收益": ret20,
        "预期20日回撤": min20,
        "决策日": bars[-1].get("date"),
        "现价": round(feats["price"], 2),
        # 趋势结构
        "窗口天数": int(feats["win"]),
        "累计涨幅%": round(feats["total_gain"], 1),
        "上涨占比%": round(feats["up_ratio"] * 100, 0),
        "最大单日涨%": round(feats["max_gain"], 1),
        "最大单日跌%": round(feats["max_loss"], 1),
        "最大回撤%": round(feats["max_pullback"] * 100, 1),
        "日涨幅std": round(feats["ret_std"], 2),
        "连阳天数": int(feats["up_streak"]),
        "创20日新高占比%": round(feats["new_high_ratio"] * 100, 0),
        # 均线
        "均线多头": bool(feats["ma_bull"]),
        "多头占比%": round(feats["ma_bull_ratio"] * 100, 0),
        "均线发散度": round(feats["ma_spread"], 3),
        "MA5斜率": round(feats["ma5_slope"], 4),
        "MA20斜率": round(feats["ma20_slope"], 4),
        "BIAS5%": round(feats["bias5"], 2),
        "BIAS10%": round(feats["bias10"], 2),
        "BIAS20%": round(feats["bias20"], 2),
        # MACD
        "MACD_DIF": ctx.dif[d],
        "MACD_DEA": ctx.dea[d],
        "MACD柱": ctx.hist[d],
        "MACD柱归一%": round(feats["hist_n"], 2),
        "红柱占比%": round(feats["hist_pos_ratio"] * 100, 0),
        "金叉后天数": int(feats["macd_days_since_cross"]),
        # 量能
        "量比(前20)": round(feats["vol_ratio"], 2),
        "量能趋势": round(feats["vol_trend"], 2),
        "涨跌量比": round(feats["vol_updown"], 2),
        # 位置
        "距60日高%": round((feats["close_vs_hh60"] - 1) * 100, 2),
        "60日涨幅%": round(feats["ret_60d"], 1),
        "120日涨幅%": round(feats["ret_120d"], 1),
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
    return rec
