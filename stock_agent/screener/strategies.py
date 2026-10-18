"""两大形态策略。

1. **美人肩**（``detect_meirenjian``）
   涨停之后股价不回落，一直围绕涨停价窄幅横盘（默认上下 4%），
   量能逐步萎缩，形似美人肩部——主力锁仓、蓄势待发。

2. **涨不停不涨停**（``detect_steady_climb``）
   连续多个交易日小幅收涨，从不涨停，也没有明显回撤，
   均线多头排列、量能温和——典型的慢牛 / 资金持续流入形态。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .indicators import (
    board_limit_ratio,
    clamp,
    is_limit_down_bar,
    is_limit_up_bar,
    is_st,
    limit_up_price,
    mean,
    moving_average,
    pct_change,
    stdev,
)

# ============================================================================
# 策略一：美人肩
# ============================================================================


@dataclass
class MeiRenJianConfig:
    """美人肩参数。

    .. deprecated::
       这是 v1 版本（只看「涨停后 ±4% 横盘」），已被
       :class:`stock_agent.screener.patterns.SecondBoardConfig` 取代。
       31 万样本统计显示旧口径的三个假设都是错的（不应奖励长横盘 / 不应要求缩量 /
       不应限制窄幅），请使用 ``detect_second_board``。

    Attributes
    ----------
    band:
        涨停价上下浮动幅度，0.04 = ±4%（用户口径）。
    lookback:
        在最近多少个交易日内寻找那根「涨停日」。
    min_days / max_days:
        涨停之后至少 / 至多整理多少个交易日。
    allow_breach:
        允许有几天轻微越界（默认 1 天），越界上限为 ``band * breach_ratio``。
    max_volume_ratio:
        整理期日均成交量 / 涨停日成交量 的上限，过滤放巨量的。
    min_limit_amount:
        涨停日成交额下限（元），过滤掉一字板缩量票。
    exclude_st / exclude_new:
        排除风险股 / 上市不足 60 个交易日的新股。
    """

    band: float = 0.04
    lookback: int = 25
    min_days: int = 3
    max_days: int = 45
    allow_breach: int = 1
    breach_ratio: float = 0.5
    max_volume_ratio: float = 1.6
    min_limit_amount: float = 2e7
    min_bars: int = 60
    exclude_st: bool = True
    exclude_new: bool = True
    require_bull_ma: bool = False


def _score_meirenjian(
    limit_price: float,
    after: Sequence[Dict[str, Any]],
    limit_volume: float,
    cfg: MeiRenJianConfig,
) -> Tuple[float, Dict[str, Any]]:
    highs = [float(b["high"]) for b in after if b.get("high") is not None]
    lows = [float(b["low"]) for b in after if b.get("low") is not None]
    closes = [float(b["close"]) for b in after if b.get("close") is not None]
    vols = [float(b.get("volume") or 0) for b in after]

    top, bottom = max(highs), min(lows)
    width = (top - bottom) / limit_price                 # 整理期总振幅
    avg_close = mean(closes)
    vol_avg = mean(vols)
    vol_ratio = (vol_avg / limit_volume) if limit_volume else 1.0

    # ① 整理时间（越长筹码越稳）
    s_days = clamp(len(after) / 20.0) * 20.0
    # ② 波动收窄（总振幅相对 2*band 越小越好）
    s_width = clamp(1.0 - width / (2 * cfg.band)) * 25.0
    # ③ 缩量程度
    s_shrink = clamp(1.0 - min(vol_ratio, 1.0)) * 25.0
    # ④ 重心贴合涨停价
    s_anchor = clamp(1.0 - abs(avg_close - limit_price) / (limit_price * cfg.band)) * 20.0
    # ⑤ 收盘位置偏上沿（强势）
    span = max(top - bottom, 1e-6)
    s_pos = clamp((closes[-1] - bottom) / span) * 10.0

    score = round(s_days + s_width + s_shrink + s_anchor + s_pos, 1)
    detail = {
        "区间最高": round(top, 2),
        "区间最低": round(bottom, 2),
        "区间振幅%": round(width * 100, 2),
        "整理期均价": round(avg_close, 2),
        "缩量比": round(vol_ratio, 2),
        "重心偏离%": round((avg_close - limit_price) / limit_price * 100, 2),
        "收盘位置%": round(clamp((closes[-1] - bottom) / span) * 100, 1),
    }
    return score, detail


def detect_meirenjian(
    bars: Sequence[Dict[str, Any]],
    code: str,
    name: str = "",
    cfg: Optional[MeiRenJianConfig] = None,
    quote: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """检测美人肩形态，命中返回详情，否则返回 ``None``。"""
    cfg = cfg or MeiRenJianConfig()
    if cfg.exclude_st and is_st(name):
        return None
    n = len(bars)
    if n < cfg.min_bars:
        return None

    ratio = board_limit_ratio(code, name)
    band = cfg.band
    hard_cap = band * (1.0 + cfg.breach_ratio)

    best: Optional[Dict[str, Any]] = None
    # 涨停日索引范围：至少留 min_days 根整理，且不超过 lookback 与 max_days
    lo = max(1, n - 1 - cfg.lookback)
    hi = n - 1 - cfg.min_days
    if hi < lo:
        return None

    for i in range(hi, lo - 1, -1):  # 从最近的涨停日往前找
        bar = bars[i]
        prev_close = bars[i - 1].get("close")
        if not is_limit_up_bar(bar, prev_close, ratio):
            continue

        limit_p = float(bar["close"])
        after = list(bars[i + 1 :])
        if len(after) < cfg.min_days or len(after) > cfg.max_days:
            continue

        # 整理期区间检查
        upper, lower = limit_p * (1 + band), limit_p * (1 - band)
        upper_hard, lower_hard = limit_p * (1 + hard_cap), limit_p * (1 - hard_cap)
        breaches = 0
        ok = True
        for b in after:
            high, low = b.get("high"), b.get("low")
            if high is None or low is None:
                ok = False
                break
            if float(high) > upper_hard or float(low) < lower_hard:
                ok = False
                break
            if float(high) > upper or float(low) < lower:
                breaches += 1
        if not ok or breaches > cfg.allow_breach:
            continue

        # 现价必须还在区间内
        last_close = bars[-1].get("close")
        if last_close is None or not (lower <= float(last_close) <= upper):
            continue

        # 整理期不能出现跌停
        for k in range(1, len(after)):
            if is_limit_down_bar(after[k], after[k - 1].get("close"), ratio):
                ok = False
                break
        if not ok:
            continue

        # 涨停日成交额门槛（过滤一字板）
        limit_amount = float(bar.get("amount") or 0)
        if limit_amount and limit_amount < cfg.min_limit_amount:
            continue

        # 缩量检查
        limit_volume = float(bar.get("volume") or 0)
        vol_after = mean([float(b.get("volume") or 0) for b in after])
        vol_ratio = (vol_after / limit_volume) if limit_volume else 1.0
        if cfg.max_volume_ratio and vol_ratio > cfg.max_volume_ratio:
            continue

        # 均线多头（可选）
        closes = [b.get("close") for b in bars]
        ma5, ma10, ma20 = (
            moving_average(closes, 5)[-1],
            moving_average(closes, 10)[-1],
            moving_average(closes, 20)[-1],
        )
        bull_ma = bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
        if cfg.require_bull_ma and not bull_ma:
            continue

        score, detail = _score_meirenjian(limit_p, after, limit_volume, cfg)
        cur_price = float(last_close)
        record = {
            "strategy": "meirenjian",
            "code": code,
            "name": name,
            "score": score,
            "涨停日": bar.get("date"),
            "涨停价": round(limit_p, 2),
            "现价": round(cur_price, 2),
            "距涨停价%": round((cur_price - limit_p) / limit_p * 100, 2),
            "整理天数": len(after),
            "允许区间": [round(lower, 2), round(upper, 2)],
            "越界天数": breaches,
            "均线多头": bull_ma,
            "MA5": ma5,
            "MA10": ma10,
            "MA20": ma20,
            **detail,
        }
        if quote:
            record.update(
                {
                    "现价": quote.get("price") or record["现价"],
                    "涨跌幅%": quote.get("change_pct"),
                    "成交额": quote.get("amount"),
                    "换手率%": quote.get("turnover_rate"),
                    "总市值": quote.get("total_mv"),
                }
            )
        if best is None or record["score"] > best["score"]:
            best = record
    return best


# ============================================================================
# 策略二：涨不停不涨停
# ============================================================================


@dataclass
class SteadyClimbConfig:
    """涨不停不涨停参数。

    Attributes
    ----------
    days:
        观察窗口（最近多少个交易日）。
    allow_down:
        允许其中几天收跌（默认 0 = 严格连续上涨）。
    max_daily_gain / min_daily_gain:
        单日涨幅上限 / 下限（%）。上限要明显小于涨停幅度。
    min_total_gain / max_total_gain:
        窗口累计涨幅区间（%）。
    max_drawdown:
        期间允许的最大单日跌幅（%）。
    min_amount:
        最新交易日成交额下限（元）。
    min_price / min_market_cap:
        低价股 / 微盘股过滤。
    volume_ratio_range:
        近期均量 / 更早均量 的合理区间，过大说明放量异动。
    min_yang_ratio:
        阳线（收 > 开）占比下限，0 表示不检查。
    require_ma_bull:
        是否要求 MA5 > MA10 > MA20 多头排列。
    """

    days: int = 8
    allow_down: int = 1
    max_daily_gain: float = 7.0
    min_daily_gain: float = 0.0
    min_total_gain: float = 3.0
    max_total_gain: float = 60.0
    max_drawdown: float = 4.0
    limit_margin: float = 0.5      # 距离涨停还差多少个百分点才算"不涨停"
    min_bars: int = 40
    exclude_st: bool = True
    exclude_new: bool = True
    min_amount: float = 3e7
    min_price: float = 2.0
    min_market_cap: float = 0.0
    volume_ratio_range: Tuple[float, float] = (0.4, 4.0)
    min_yang_ratio: float = 0.0
    require_ma_bull: bool = True


def detect_steady_climb(
    bars: Sequence[Dict[str, Any]],
    code: str,
    name: str = "",
    cfg: Optional[SteadyClimbConfig] = None,
    quote: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """检测「涨不停不涨停」形态，命中返回详情，否则返回 ``None``。"""
    cfg = cfg or SteadyClimbConfig()
    if cfg.exclude_st and is_st(name):
        return None
    n = len(bars)
    if n < max(cfg.min_bars, cfg.days + 1, 25):
        return None

    limit_pct = board_limit_ratio(code, name) * 100.0
    cap = min(cfg.max_daily_gain, limit_pct - cfg.limit_margin)

    window = list(bars[-(cfg.days + 1) :])
    ratios: List[float] = []
    for k in range(1, len(window)):
        r = pct_change(window[k].get("close"), window[k - 1].get("close"))
        if r is None:
            return None
        ratios.append(r)

    # ① 上涨天数
    up_days = sum(1 for r in ratios if r > 0)
    if up_days < cfg.days - cfg.allow_down:
        return None
    # ② 不涨停
    if any(r >= limit_pct - cfg.limit_margin for r in ratios):
        return None
    # ③ 单日涨幅上限（"一点点"）
    if any(r > cap for r in ratios):
        return None
    # ④ 单日涨幅下限
    if any(0 < r < cfg.min_daily_gain for r in ratios):
        return None
    # ⑤ 回撤控制
    if any(r < -cfg.max_drawdown for r in ratios):
        return None
    # ⑥ 无跌停
    for k in range(1, len(window)):
        if is_limit_down_bar(window[k], window[k - 1].get("close"), limit_pct / 100.0):
            return None

    total_gain = pct_change(window[-1].get("close"), window[0].get("close"))
    if total_gain is None or not (cfg.min_total_gain <= total_gain <= cfg.max_total_gain):
        return None

    # ⑦ 量能温和
    recent_vol = mean([float(b.get("volume") or 0) for b in window[1:]])
    base_start = max(0, n - cfg.days * 3)
    base_vol = mean([float(b.get("volume") or 0) for b in bars[base_start : n - cfg.days]])
    vol_ratio = (recent_vol / base_vol) if base_vol else 1.0
    lo_v, hi_v = cfg.volume_ratio_range
    if not (lo_v <= vol_ratio <= hi_v):
        return None

    # ⑧ 阳线占比
    yang = sum(1 for b in window[1:] if (b.get("close") or 0) >= (b.get("open") or 0))
    yang_ratio = yang / max(1, len(window) - 1)
    if cfg.min_yang_ratio and yang_ratio < cfg.min_yang_ratio:
        return None

    # ⑨ 均线多头
    closes = [b.get("close") for b in bars]
    ma5 = moving_average(closes, 5)[-1]
    ma10 = moving_average(closes, 10)[-1]
    ma20 = moving_average(closes, 20)[-1]
    bull_ma = bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
    if cfg.require_ma_bull and not bull_ma:
        return None

    # 基本面 / 流动性门槛
    price = window[-1].get("close") or 0
    if price < cfg.min_price:
        return None
    amount = float(window[-1].get("amount") or 0)
    if amount and amount < cfg.min_amount:
        return None
    if cfg.min_market_cap and quote:
        mv = quote.get("total_mv") or 0
        if mv and mv < cfg.min_market_cap:
            return None

    # ------------------------------------------------ 打分
    avg_r = mean(ratios)
    cv = (stdev(ratios) / avg_r) if avg_r else 9.9
    s_days = clamp(cfg.days / 12.0) * 25.0
    s_even = clamp(1.0 - cv / 1.2) * 20.0
    s_total = clamp(1.0 - abs(total_gain - 15.0) / 30.0) * 15.0
    if bull_ma:
        s_ma = 25.0
    elif ma5 and ma10 and ma5 > ma10:
        s_ma = 15.0
    else:
        s_ma = 5.0
    s_vol = clamp(1.0 - abs(vol_ratio - 1.2) / 2.5) * 15.0
    score = round(s_days + s_even + s_total + s_ma + s_vol, 1)

    record: Dict[str, Any] = {
        "strategy": "steady_climb",
        "code": code,
        "name": name,
        "score": score,
        "连涨天数": cfg.days,
        "实际上涨天数": up_days,
        "起始日": window[0].get("date"),
        "起始价": round(float(window[0].get("close") or 0), 2),
        "现价": round(float(price), 2),
        "累计涨幅%": round(total_gain, 2),
        "日均涨幅%": round(avg_r, 2),
        "单日最大涨幅%": round(max(ratios), 2),
        "单日最大跌幅%": round(min(ratios), 2),
        "涨幅波动": round(cv, 2),
        "阳线占比%": round(yang_ratio * 100, 1),
        "量能比": round(vol_ratio, 2),
        "均线多头": bull_ma,
        "MA5": ma5,
        "MA10": ma10,
        "MA20": ma20,
        "涨停幅度%": round(limit_pct, 1),
    }
    if quote:
        record.update(
            {
                "现价": quote.get("price") or record["现价"],
                "涨跌幅%": quote.get("change_pct"),
                "成交额": quote.get("amount"),
                "换手率%": quote.get("turnover_rate"),
                "总市值": quote.get("total_mv"),
            }
        )
    return record


def config_to_dict(cfg: Any) -> Dict[str, Any]:
    return asdict(cfg)
