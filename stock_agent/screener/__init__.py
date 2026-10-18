"""选股器：本地缓存 + 形态策略 + 并发扫描。

模块划分::

    store.py         SQLite 本地缓存（日线、股票池、键值）
    indicators.py    涨停价计算、均线、涨跌幅等基础指标
    strategies.py    美人肩 / 涨不停不涨停 两个形态策略
    scanner.py       调度：候选池 → 并发取数 → 判定 → 打分 → 排序
    cli.py           命令行入口

命令行::

    python -m stock_agent.screener meirenjian        # 美人肩
    python -m stock_agent.screener steady            # 涨不停不涨停
    python -m stock_agent.screener warmup            # 预热全市场日线缓存
"""

from .indicators import (
    board_limit_ratio,
    is_limit_up_bar,
    is_st,
    limit_up_price,
    moving_average,
)
from .scanner import Screener, ScanProgress
from .store import ScreenerStore
from .strategies import (
    MeiRenJianConfig,
    SteadyClimbConfig,
    detect_meirenjian,
    detect_steady_climb,
)

__all__ = [
    "Screener",
    "ScanProgress",
    "ScreenerStore",
    "MeiRenJianConfig",
    "SteadyClimbConfig",
    "detect_meirenjian",
    "detect_steady_climb",
    "board_limit_ratio",
    "is_limit_up_bar",
    "is_st",
    "limit_up_price",
    "moving_average",
]
