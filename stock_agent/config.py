"""全局配置。

所有配置均可通过环境变量覆盖，例如::

    set STOCK_PORT=9000
    set STOCK_QUOTE_INTERVAL=2
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Tuple


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


# 常用指数（secid, 名称）。secid 前缀 1=上交所, 0=深交所
DEFAULT_INDICES: Tuple[Tuple[str, str], ...] = (
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000300", "沪深300"),
    ("1.000688", "科创50"),
    ("0.899050", "北证50"),
    ("0.399905", "中证500"),
)


@dataclass
class Settings:
    """运行期配置。"""

    # --- HTTP 服务 ---
    host: str = field(default_factory=lambda: os.getenv("STOCK_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("STOCK_PORT", 8000))
    reload: bool = field(default_factory=lambda: _env_bool("STOCK_RELOAD", False))

    # --- 上游请求 ---
    http_timeout: float = field(default_factory=lambda: _env_float("STOCK_HTTP_TIMEOUT", 8.0))
    http_retries: int = field(default_factory=lambda: _env_int("STOCK_HTTP_RETRIES", 2))
    max_concurrency: int = field(default_factory=lambda: _env_int("STOCK_MAX_CONCURRENCY", 16))
    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "STOCK_UA",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
    )

    # --- 缓存 TTL（秒） ---
    ttl_quote: float = field(default_factory=lambda: _env_float("STOCK_TTL_QUOTE", 3.0))
    ttl_minute: float = field(default_factory=lambda: _env_float("STOCK_TTL_MINUTE", 10.0))
    ttl_kline: float = field(default_factory=lambda: _env_float("STOCK_TTL_KLINE", 60.0))
    ttl_list: float = field(default_factory=lambda: _env_float("STOCK_TTL_LIST", 20.0))
    ttl_search: float = field(default_factory=lambda: _env_float("STOCK_TTL_SEARCH", 600.0))

    # --- WebSocket 推送 ---
    ws_interval: float = field(default_factory=lambda: _env_float("STOCK_WS_INTERVAL", 3.0))
    ws_max_codes: int = field(default_factory=lambda: _env_int("STOCK_WS_MAX_CODES", 60))

    # --- 数据源 ---
    primary_source: str = field(default_factory=lambda: os.getenv("STOCK_SOURCE", "eastmoney"))
    enable_sina_fallback: bool = field(
        default_factory=lambda: _env_bool("STOCK_SINA_FALLBACK", True)
    )
    #: 强制数据源优先级，例如 "tencent,eastmoney"（空 = 用内置默认链）
    source_order: str = field(default_factory=lambda: os.getenv("STOCK_SOURCE_ORDER", ""))
    #: 熔断：连续失败多少次后短暂跳过该数据源
    breaker_threshold: int = field(
        default_factory=lambda: _env_int("STOCK_BREAKER_THRESHOLD", 3)
    )
    breaker_cooldown: float = field(
        default_factory=lambda: _env_float("STOCK_BREAKER_COOLDOWN", 60.0)
    )

    # --- 指数列表 ---
    indices: Tuple[Tuple[str, str], ...] = DEFAULT_INDICES

    # --- 静态文件目录 ---
    web_dir: str = field(
        default_factory=lambda: os.getenv(
            "STOCK_WEB_DIR",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"),
        )
    )

    # --- 应用数据目录（选股器缓存等） ---
    data_dir: str = field(
        default_factory=lambda: os.getenv(
            "STOCK_DATA_DIR",
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".data"
            ),
        )
    )

    @property
    def index_map(self) -> Dict[str, str]:
        return {secid: name for secid, name in self.indices}

    @property
    def index_secids(self) -> List[str]:
        return [secid for secid, _ in self.indices]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
