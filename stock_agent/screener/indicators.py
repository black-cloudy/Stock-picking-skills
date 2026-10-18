"""选股基础指标：涨跌幅、涨停价、均线、ST 判定。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

# 各板块涨跌幅限制
_LIMIT_MAIN = 0.10
_LIMIT_GEM = 0.20     # 创业板 300/301、科创板 688/689
_LIMIT_BJ = 0.30      # 北交所 4xxxxx / 8xxxxx / 920xxx
_LIMIT_ST = 0.05      # 主板 ST


def is_st(name: str) -> bool:
    """名称中含 ST / *ST / S*ST / 退 视为风险股。"""
    n = (name or "").upper().replace(" ", "")
    return "ST" in n or "退" in (name or "")


def board_limit_ratio(code: str, name: str = "") -> float:
    """返回该股票的涨跌停幅度（小数，如 0.10）。"""
    code = str(code or "")
    if code.startswith(("300", "301", "688", "689")):
        return _LIMIT_GEM
    if code.startswith(("4", "8", "920")):
        return _LIMIT_BJ
    if is_st(name):
        return _LIMIT_ST
    return _LIMIT_MAIN


def limit_up_price(pre_close: float, ratio: float) -> float:
    """按交易所规则四舍五入到分。"""
    return round(float(pre_close) * (1.0 + ratio) + 1e-9, 2)


def limit_down_price(pre_close: float, ratio: float) -> float:
    return round(float(pre_close) * (1.0 - ratio) + 1e-9, 2)


def pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old in (None, 0):
        return None
    return (float(new) - float(old)) / float(old) * 100.0


def is_limit_up_bar(
    bar: Dict[str, Any],
    prev_close: Optional[float],
    ratio: float,
    tol: float = 0.004,
) -> bool:
    """判断某根日 K 是否为「收盘涨停」。

    容差默认 0.4%，用于吸收四舍五入与复权误差。
    """
    close = bar.get("close")
    if close is None or not prev_close:
        return False
    target = limit_up_price(prev_close, ratio)
    if abs(float(close) - target) <= max(0.011, target * tol):
        return True
    # 浮点/复权导致的小偏差：用涨幅兜底
    chg = (float(close) - float(prev_close)) / float(prev_close)
    return chg >= ratio - 0.004


def is_limit_down_bar(bar: Dict[str, Any], prev_close: Optional[float], ratio: float) -> bool:
    close = bar.get("close")
    if close is None or not prev_close:
        return False
    target = limit_down_price(prev_close, ratio)
    return abs(float(close) - target) <= max(0.011, target * 0.004)


def moving_average(values: Sequence[Optional[float]], window: int) -> List[Optional[float]]:
    """简单移动平均，前 window-1 个返回 None。"""
    out: List[Optional[float]] = []
    total = 0.0
    for i, v in enumerate(values):
        if v is None:
            out.append(None)
            continue
        total += float(v)
        if i >= window:
            prev = values[i - window]
            if prev is not None:
                total -= float(prev)
        out.append(round(total / window, 4) if i >= window - 1 else None)
    return out


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def mean(values: Sequence[float]) -> float:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def stdev(values: Sequence[float]) -> float:
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


def ema(values: Sequence[Optional[float]], span: int) -> List[Optional[float]]:
    """指数移动平均（与通达信/同花顺一致：首值用第一个有效值初始化）。"""
    k = 2.0 / (span + 1.0)
    out: List[Optional[float]] = []
    prev: Optional[float] = None
    for v in values:
        if v is None:
            out.append(prev)
            continue
        prev = float(v) if prev is None else float(v) * k + prev * (1.0 - k)
        out.append(prev)
    return out


def macd(
    closes: Sequence[Optional[float]], fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """MACD 指标，返回 ``(DIF, DEA, MACD柱)``。

    MACD 柱按 A 股习惯为 ``(DIF - DEA) * 2``。
    """
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    dif: List[Optional[float]] = [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(fast_ema, slow_ema)
    ]
    dea = ema([d if d is not None else 0.0 for d in dif], signal)
    hist: List[Optional[float]] = [
        ((d - e) * 2.0) if (d is not None and e is not None) else None
        for d, e in zip(dif, dea)
    ]
    return dif, dea, hist


def slope(values: Sequence[Optional[float]], window: int = 3) -> Optional[float]:
    """最近 ``window`` 根的相对变化率（用于判断均线/指标的拐头方向）。"""
    vals = [v for v in values if v is not None]
    if len(vals) < window + 1:
        return None
    prev, cur = vals[-window - 1], vals[-1]
    if not prev:
        return None
    return (cur - prev) / abs(prev)


def percentile_rank(values: Sequence[float], x: float) -> float:
    """``x`` 在序列中的分位（0~1）。"""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 0.0
    return sum(1 for v in vals if v <= x) / len(vals)


def rolling_max(
    values: Sequence[Optional[float]], window: int
) -> List[Optional[float]]:
    """滚动最大值（含当前），前 window-1 个用部分窗口。"""
    out: List[Optional[float]] = []
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        chunk = [v for v in values[lo : i + 1] if v is not None]
        out.append(max(chunk) if chunk else None)
    return out


def rolling_max_fast(
    values: Sequence[Optional[float]], window: int
) -> List[Optional[float]]:
    """单调队列实现 O(n) 的滚动最大值。"""
    from collections import deque

    n = len(values)
    out: List[Optional[float]] = [None] * n
    dq: deque = deque()  # 存下标，值单调递减
    for i in range(n):
        v = values[i]
        while dq and (values[dq[-1]] is None or (v is not None and values[dq[-1]] <= v)):
            dq.pop()
        dq.append(i)
        while dq and dq[0] < i - window + 1:
            dq.popleft()
        if dq and values[dq[0]] is not None:
            out[i] = values[dq[0]]
    return out
