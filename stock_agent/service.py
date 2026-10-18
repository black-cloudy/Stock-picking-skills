"""业务服务层：把数据源、缓存、交易时段判断组合成对上层友好的 API。

特性
----
* **多数据源故障转移**：按能力声明优先级链（行情 / 分时 / K 线 / 盘口 …）
* **熔断降级**：某个源连续失败 3 次自动熔断 60 秒，避免每次请求都白等
* **TTL 缓存 + 单飞**：同一 key 的并发请求只打一次上游
* **数据源顺序可配**：``STOCK_SOURCE_ORDER="tencent,eastmoney"`` 可强制优先腾讯
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cache import TTLCache, get_cache
from .config import Settings, get_settings
from .providers import (
    DataSourceError,
    EastMoneyProvider,
    SinaProvider,
    TencentProvider,
)
from .utils import (
    PHASE_LABEL,
    is_trading,
    market_phase,
    now_cn,
    to_secid,
)

log = logging.getLogger(__name__)

# 常用周期别名
PERIOD_ALIAS = {
    "1": "1m", "1min": "1m", "min": "1m", "minute": "1m",
    "5": "5m", "5min": "5m",
    "15": "15m", "15min": "15m",
    "30": "30m", "30min": "30m",
    "60": "60m", "60min": "60m", "1h": "60m",
    "d": "daily", "day": "daily", "daily": "daily", "日k": "daily", "日K": "daily",
    "w": "weekly", "week": "weekly", "weekly": "weekly", "周k": "weekly", "周K": "weekly",
    "m": "monthly", "month": "monthly", "monthly": "monthly", "月k": "monthly", "月K": "monthly",
}

# 各能力的默认数据源优先级
DEFAULT_CHAINS: Dict[str, Tuple[str, ...]] = {
    "quote": ("eastmoney", "tencent", "sina"),
    "quotes": ("eastmoney", "tencent", "sina"),
    "minute": ("eastmoney", "tencent"),
    "kline": ("eastmoney", "tencent", "sina"),
    "orderbook": ("sina", "tencent"),
    "ticks": ("eastmoney", "tencent"),
    "list": ("eastmoney", "sina"),
    "search": ("eastmoney", "tencent"),
}


class CircuitBreaker:
    """连续失败熔断器。

    区分「硬失败」（网络/协议错误）与「软失败」（返回了空数据）：
    软失败需要更多次才会熔断，避免因为一两个退市/新股代码误伤整个数据源。
    """

    __slots__ = ("threshold", "cooldown", "fails", "soft", "opened_at")

    def __init__(self, threshold: int = 3, cooldown: float = 60.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.fails = 0
        self.soft = 0
        self.opened_at = 0.0

    @property
    def soft_limit(self) -> int:
        return max(self.threshold * 3, self.threshold + 2)

    @property
    def is_tripped(self) -> bool:
        return self.fails >= self.threshold or self.soft >= self.soft_limit

    def allow(self, now: float) -> bool:
        if not self.is_tripped:
            return True
        if now - self.opened_at >= self.cooldown:
            self.fails = 0  # 半开：放行一次探测
            self.soft = 0
            return True
        return False

    def record(self, ok: bool, now: float, soft: bool = False) -> None:
        if ok:
            self.fails = 0
            self.soft = 0
            return
        if soft:
            self.soft += 1
        else:
            self.fails += 1
        if self.is_tripped:
            self.opened_at = now


class MarketService:
    """A 股行情服务（多数据源自动容错）。"""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        cache: Optional[TTLCache] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cache = cache or get_cache()
        self.em = EastMoneyProvider(self.settings)
        self.tencent = TencentProvider(self.settings)
        self.sina = SinaProvider(self.settings) if self.settings.enable_sina_fallback else None
        self._by_name = {
            p.name: p for p in (self.em, self.tencent, self.sina) if p is not None
        }
        self._providers = list(self._by_name.values())
        self._breakers: Dict[str, CircuitBreaker] = {}
        # 数据源探测结果缓存： key -> (时间, 顺序)
        self._order_cache: Dict[str, tuple] = {}
        self.ORDER_TTL = 600.0
        # 允许通过 settings 覆盖顺序，例如 "tencent,eastmoney"
        self._override = [s.strip() for s in (self.settings.source_order or "").split(",") if s.strip()]
        self.stats: Dict[str, int] = {"requests": 0, "failovers": 0, "rejected": 0}

    # ------------------------------------------------------------ 生命周期

    async def __aenter__(self) -> "MarketService":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await asyncio.gather(*(p.close() for p in self._providers), return_exceptions=True)

    # ------------------------------------------------------------ 数据源编排

    def _chain(self, capability: str) -> List[Any]:
        """返回该能力下按优先级排序的数据源实例。"""
        names = self._override or list(DEFAULT_CHAINS.get(capability, ()))
        # 未在覆盖列表中提到的源补在后面，保证不会漏
        tail = [n for n in DEFAULT_CHAINS.get(capability, ()) if n not in names]
        return [self._by_name[n] for n in (names + tail) if n in self._by_name]

    def force_sources(self, order: Sequence[str]) -> None:
        """临时强制数据源顺序（选股器等批量任务用）。"""
        self._override = [s.strip() for s in order if s and s.strip() in self._by_name]

    def set_concurrency(self, limit: int) -> None:
        """调整上游并发上限（批量扫描时调大，日常使用保持默认）。"""
        limit = max(1, int(limit))
        for provider in self._providers:
            provider._sem = asyncio.Semaphore(limit)  # noqa: SLF001 - 内部调优

    async def auto_order(
        self,
        capability: str = "kline",
        sample: str = "600519",
        timeout: float = 4.0,
        adjust: str = "qfq",
        use_cache: bool = True,
    ) -> List[str]:
        """并发探测各数据源，把可用的排在前面并写入顺序。

        结果会缓存 ``ORDER_TTL`` 秒，避免每次批量任务都花 2~3 秒探测。
        """
        key = f"{capability}:{adjust}"
        now = time.monotonic()
        if use_cache:
            hit = self._order_cache.get(key)
            if hit and (now - hit[0]) < self.ORDER_TTL:
                self.force_sources(hit[1])
                return list(hit[1])

        candidates = [p for p in self._chain(capability)]
        if not candidates:
            return []
        secid = to_secid(sample)

        async def probe(provider: Any) -> Tuple[str, bool, float]:
            began = time.monotonic()
            try:
                if capability in ("quote", "quotes"):
                    call = provider.quote(secid)
                elif capability == "minute":
                    call = provider.minute(secid, 1)
                elif capability == "kline":
                    # 用实际会使用的复权方式探测，避免把「不支持前复权」的源排到前面
                    call = provider.kline(secid, "daily", adjust, 20)
                elif capability == "list":
                    call = provider.stock_list(1, 10, "pct", "desc", "all")
                elif capability == "search":
                    call = provider.search("600519", 3)
                elif capability == "ticks":
                    call = provider.ticks(secid, 5)
                else:
                    return provider.name, False, 99.0
                result = await asyncio.wait_for(call, timeout=timeout)
                ok = bool(result) and bool(
                    result.get("bars") if capability == "kline"
                    else result.get("items") if capability == "list"
                    else result
                )
            except Exception:  # noqa: BLE001
                ok = False
            return provider.name, ok, time.monotonic() - began

        results = await asyncio.gather(*(probe(p) for p in candidates))
        good = sorted([r for r in results if r[1]], key=lambda r: r[2])
        bad = [r[0] for r in results if not r[1]]
        order = [r[0] for r in good] + bad
        if order:
            self.force_sources(order)
            self._order_cache[key] = (time.monotonic(), list(order))
        log.info(
            "数据源探测 [%s%s]: %s",
            capability,
            f"/{adjust}" if capability == "kline" else "",
            ", ".join(f"{n}{'✓' if ok else '✗'}({ms * 1000:.0f}ms)" for n, ok, ms in results),
        )
        return order

    def _breaker(self, provider_name: str, capability: str) -> CircuitBreaker:
        key = f"{provider_name}:{capability}"
        br = self._breakers.get(key)
        if br is None:
            br = CircuitBreaker(
                threshold=self.settings.breaker_threshold,
                cooldown=self.settings.breaker_cooldown,
            )
            self._breakers[key] = br
        return br

    def breaker_report(self) -> Dict[str, Dict[str, Any]]:
        now = time.monotonic()
        out: Dict[str, Dict[str, Any]] = {}
        for key, br in self._breakers.items():
            if br.fails == 0 and br.soft == 0:
                continue
            out[key] = {
                "fails": br.fails,
                "soft": br.soft,
                "open": not br.allow(now),
                "cooldown_left": (
                    round(max(0.0, br.cooldown - (now - br.opened_at)), 1)
                    if br.is_tripped
                    else 0.0
                ),
            }
        return out

    async def _failover(
        self,
        what: str,
        capability: str,
        builders: List[Tuple[Any, Callable[[], Any]]],
        valid: Optional[Callable[[Any], bool]] = None,
    ) -> Any:
        """依次尝试各数据源，返回第一个有效结果。"""
        now = time.monotonic()
        errors: List[str] = []
        for provider, call in builders:
            if provider is None or call is None:
                continue
            self.stats["requests"] += 1
            br = self._breaker(provider.name, capability)
            if not br.allow(now):
                errors.append(f"{provider.name}(熔断中)")
                self.stats["rejected"] += 1
                continue
            try:
                result = await call()
            except Exception as exc:  # noqa: BLE001
                br.record(False, time.monotonic())
                errors.append(f"{provider.name}({type(exc).__name__})")
                log.debug("%s 源 %s 失败: %s", what, provider.name, exc)
                continue
            if result is None or (valid is not None and not valid(result)):
                br.record(False, time.monotonic(), soft=True)
                errors.append(f"{provider.name}(空数据)")
                continue
            br.record(True, time.monotonic())
            if errors:
                self.stats["failovers"] += 1
                log.info("%s -> 降级源 %s（%s）", what, provider.name, "; ".join(errors))
            return result
        raise DataSourceError(f"{what} 所有数据源均失败 -> " + "; ".join(errors))

    def _builders(
        self, capability: str, make: Callable[[Any], Callable[[], Any]]
    ) -> List[Tuple[Any, Callable[[], Any]]]:
        return [(p, make(p)) for p in self._chain(capability)]

    # ------------------------------------------------------------ 工具

    @staticmethod
    def normalize_period(period: str) -> str:
        key = str(period or "daily").strip()
        return PERIOD_ALIAS.get(key, PERIOD_ALIAS.get(key.lower(), key.lower()))

    def market_status(self) -> Dict[str, Any]:
        now = now_cn()
        phase = market_phase(now)
        return {
            "phase": phase,
            "label": PHASE_LABEL.get(phase, phase),
            "trading": is_trading(now),
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "sources": [p.name for p in self._providers],
            "order": {cap: [p.name for p in self._chain(cap)] for cap in DEFAULT_CHAINS},
            "breakers": self.breaker_report(),
            "stats": dict(self.stats),
            "cache": self.cache.stats(),
        }

    # ------------------------------------------------------------ 行情

    async def quote(self, code: str, with_orderbook: bool = False) -> Dict[str, Any]:
        secid = to_secid(code)

        async def load() -> Dict[str, Any]:
            return await self._failover(
                f"行情 {secid}",
                "quote",
                self._builders("quote", lambda p: (lambda: p.quote(secid))),
                valid=lambda r: bool(r and r.get("name")),
            )

        row = await self.cache.get_or_set(f"quote:{secid}", self.settings.ttl_quote, load)

        if with_orderbook:
            book = None
            if isinstance(row.get("orderbook"), dict) and row["orderbook"].get("bids"):
                book = row["orderbook"]
            else:
                fetched = await self.orderbook(secid)
                if fetched:
                    book = {"bids": fetched["bids"], "asks": fetched["asks"]}
            if book:
                row = {**row, "orderbook": book}
        return row

    async def quotes(self, codes: Sequence[str]) -> List[Dict[str, Any]]:
        secids: List[str] = []
        for c in codes:
            s = to_secid(c)
            if s not in secids:
                secids.append(s)
        if not secids:
            return []

        pending = [s for s in secids if self.cache.get(f"quote:{s}") is None]
        for s in secids:
            if self.cache.get(f"quote:{s}") is not None:
                self.cache.hits += 1

        if pending:
            rows = await self._failover(
                f"批量行情({len(pending)}只)",
                "quotes",
                self._builders("quotes", lambda p: (lambda: p.quotes(pending))),
                valid=lambda r: bool(r),
            )
            for row in rows or []:
                if row.get("secid"):
                    self.cache.set(f"quote:{row['secid']}", row, self.settings.ttl_quote)

        result: List[Dict[str, Any]] = []
        for s in secids:
            row = self.cache.get(f"quote:{s}")
            if row is not None:
                result.append(row)
        return result

    async def orderbook(self, code: str) -> Optional[Dict[str, Any]]:
        secid = to_secid(code)

        async def load() -> Optional[Dict[str, Any]]:
            async def from_sina() -> Optional[Dict[str, Any]]:
                if self.sina is None:
                    return None
                return await self.sina.orderbook(secid)

            async def from_tencent() -> Optional[Dict[str, Any]]:
                row = await self.tencent.quote(secid)
                if not row or not row.get("orderbook"):
                    return None
                book = row["orderbook"]
                if not any(b.get("price") for b in book["bids"]):
                    return None
                return {
                    "secid": secid,
                    "code": row.get("code"),
                    "name": row.get("name"),
                    "price": row.get("price"),
                    "pre_close": row.get("pre_close"),
                    "date": (row.get("time") or "")[:10],
                    "time": (row.get("time") or "")[11:],
                    "bids": book["bids"],
                    "asks": book["asks"],
                    "source": "tencent",
                }

            for name in (self.settings.source_order or "sina,tencent").split(","):
                name = name.strip()
                try:
                    if name == "sina":
                        book = await from_sina()
                    elif name == "tencent":
                        book = await from_tencent()
                    else:
                        continue
                except Exception as exc:  # noqa: BLE001
                    log.debug("盘口源 %s 失败: %s", name, exc)
                    continue
                if book and any(b.get("price") for b in book.get("bids", [])):
                    return book
            return None

        return await self.cache.get_or_set(f"book:{secid}", self.settings.ttl_quote, load)

    async def ticks(self, code: str, limit: int = 60) -> List[Dict[str, Any]]:
        secid = to_secid(code)
        return await self.cache.get_or_set(
            f"ticks:{secid}:{limit}",
            self.settings.ttl_quote,
            lambda: self._failover(
                f"逐笔 {secid}",
                "ticks",
                self._builders("ticks", lambda p: (lambda: p.ticks(secid, limit))),
                valid=lambda r: bool(r),
            ),
        )

    # ------------------------------------------------------------ 分时 / K 线

    async def minute(self, code: str, days: int = 1) -> Dict[str, Any]:
        secid = to_secid(code)
        days = max(1, min(int(days), 5))

        async def load() -> Dict[str, Any]:
            errors: List[str] = []
            best: Optional[Dict[str, Any]] = None
            best_days = 0
            for provider in self._chain("minute"):
                try:
                    res = await asyncio.wait_for(
                        provider.minute(secid, days), timeout=self.settings.http_timeout * 2
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{provider.name}({type(exc).__name__})")
                    continue
                if not res or not res.get("count"):
                    errors.append(f"{provider.name}(空数据)")
                    continue
                got = len({(b.get("time") or "")[:10] for b in res.get("trends", [])})
                if got > best_days:
                    best, best_days = res, got
                if got >= days:
                    return res
            if best is not None:
                return best
            raise DataSourceError(f"分时 {secid} 所有数据源均失败 -> " + "; ".join(errors))

        return await self.cache.get_or_set(
            f"minute:{secid}:{days}", self.settings.ttl_minute, load
        )

    async def kline(
        self,
        code: str,
        period: str = "daily",
        adjust: str = "qfq",
        limit: int = 250,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> Dict[str, Any]:
        secid = to_secid(code)
        period = self.normalize_period(period)
        limit = max(1, min(int(limit), 5000))
        key = f"kline:{secid}:{period}:{adjust}:{limit}:{start}:{end}"
        return await self.cache.get_or_set(
            key,
            self.settings.ttl_kline,
            lambda: self._failover(
                f"K线 {secid} {period}",
                "kline",
                self._builders(
                    "kline",
                    lambda p: (lambda: p.kline(secid, period, adjust, limit, start, end)),
                ),
                valid=lambda r: bool(r and r.get("bars")),
            ),
        )

    # ------------------------------------------------------------ 列表 / 搜索

    async def stock_list(
        self,
        page: int = 1,
        page_size: int = 50,
        sort_field: str = "pct",
        order: str = "desc",
        market: str = "all",
    ) -> Dict[str, Any]:
        key = f"list:{market}:{sort_field}:{order}:{page}:{page_size}"

        async def load() -> Dict[str, Any]:
            try:
                return await self._failover(
                    f"排行榜 {market}",
                    "list",
                    self._builders(
                        "list",
                        lambda p: (lambda: p.stock_list(
                            page, page_size, sort_field, order, market)),
                    ),
                    valid=lambda r: bool(r and r.get("items")),
                )
            except DataSourceError:
                log.warning("排行榜数据源均失败，用全市场快照本地排序兜底")
                return await self._local_stock_list(
                    page, page_size, sort_field, order, market)

        return await self.cache.get_or_set(key, self.settings.ttl_list, load)

    _SORT_KEYS: Dict[str, str] = {
        "pct": "change_pct", "price": "price", "amount": "amount",
        "turnover": "turnover_rate", "volume_ratio": "volume_ratio",
        "main_inflow": "main_inflow", "total_mv": "total_mv",
        "volume": "volume", "code": "code",
    }

    @staticmethod
    def _match_market(code: str, market: str) -> bool:
        if market in ("all", ""):
            return True
        if market == "sh":
            return code.startswith("6")
        if market == "sz":
            return code.startswith(("0", "3"))
        if market == "bj":
            return code.startswith(("4", "8", "920"))
        if market == "cyb":
            return code.startswith(("300", "301"))
        if market == "kcb":
            return code.startswith(("688", "689"))
        if market == "main":
            return code.startswith(("000", "001", "002", "003",
                                    "600", "601", "603", "605"))
        return True

    async def _local_stock_list(
        self, page: int, page_size: int, sort_field: str, order: str, market: str
    ) -> Dict[str, Any]:
        """用全市场快照在本地排序 / 分页（兜底用）。"""
        rows = [r for r in await self.all_stocks(market)
                if self._match_market(r.get("code") or "", market)]
        key = self._SORT_KEYS.get(sort_field, "change_pct")
        numbered = [r for r in rows if r.get(key) is not None]
        numbered.sort(key=lambda r: r[key], reverse=(order == "desc"))
        total = len(numbered)
        start = max(0, (page - 1) * page_size)
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if page_size else 0,
            "market": market,
            "sort_field": sort_field,
            "order": order,
            "delayed": False,
            "source": "local",
            "items": numbered[start:start + page_size],
        }

    async def all_stocks(self, market: str = "all", page_size: int = 100) -> List[Dict[str, Any]]:
        """拉取整个市场快照（东财 -> 新浪 -> 本地缓存 三级兜底）。"""

        async def load() -> List[Dict[str, Any]]:
            try:
                return await self._failover(
                    "全市场列表",
                    "list",
                    self._builders(
                        "list",
                        lambda p: (lambda: p.all_stocks(market=market, page_size=page_size)),
                    ),
                    valid=lambda r: isinstance(r, list) and len(r) > 200,
                )
            except DataSourceError:
                log.warning("全市场列表所有数据源均失败，回退到本地缓存")
                return self._local_universe()

        return await self.cache.get_or_set(
            f"universe:{market}", self.settings.ttl_list, load
        )

    def _local_universe(self) -> List[Dict[str, Any]]:
        """上游全挂时的本地兜底：只用本地缓存里的代码 + 名称（无实时行情）。"""
        try:
            import json as _json
            import os as _os

            from .screener.scanner import default_store_path
            from .screener.store import ScreenerStore

            store = ScreenerStore(default_store_path(self.settings))
            names: Dict[str, str] = {}
            meta_path = _os.path.join(self.settings.data_dir, "meta.json")
            if _os.path.exists(meta_path):
                try:
                    with open(meta_path, encoding="utf-8") as fh:
                        names = {c: (v.get("name") or "")
                                 for c, v in _json.load(fh).items()}
                except Exception:  # noqa: BLE001
                    names = {}
            out: List[Dict[str, Any]] = []
            for code in store.all_codes():
                mk = 1 if code.startswith(("6", "9")) else 0
                out.append({
                    "code": code, "secid": f"{mk}.{code}", "market": mk,
                    "market_name": "SH" if mk == 1 else "SZ",
                    "name": names.get(code, ""),
                    "price": None, "change_pct": None, "change": None,
                    "volume": None, "amount": None, "amplitude": None,
                    "turnover_rate": None, "pe_dynamic": None, "volume_ratio": None,
                    "high": None, "low": None, "open": None, "pre_close": None,
                    "total_mv": None, "float_mv": None, "speed": None, "pb": None,
                    "change_60d": None, "change_ytd": None, "main_inflow": None,
                    "pe_ttm": None, "industry": None, "status": None,
                })
            log.info("本地兜底列表：%d 只", len(out))
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("本地兜底列表失败: %s", exc)
            return []

    async def search(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        key = f"search:{keyword.lower()}:{limit}"

        async def load() -> List[Dict[str, Any]]:
            try:
                return await self._failover(
                    f"搜索 {keyword}",
                    "search",
                    self._builders("search", lambda p: (lambda: p.search(keyword, limit))),
                    valid=lambda r: bool(r),
                )
            except DataSourceError:
                return []

        return await self.cache.get_or_set(key, self.settings.ttl_search, load)

    async def indices(self) -> List[Dict[str, Any]]:
        rows = await self.quotes(self.settings.index_secids)
        name_map = self.settings.index_map
        for row in rows:
            row["index_name"] = name_map.get(row["secid"], row.get("name"))
        order = {secid: i for i, secid in enumerate(self.settings.index_secids)}
        rows.sort(key=lambda r: order.get(r["secid"], 999))
        return rows

    # ------------------------------------------------------------ 聚合快照

    async def snapshot(
        self,
        code: str,
        period: str = "daily",
        adjust: str = "qfq",
        kline_limit: int = 250,
        minute_days: int = 1,
        with_orderbook: bool = True,
        with_ticks: bool = False,
    ) -> Dict[str, Any]:
        """一次性拉取「打开一只股票」所需的全部数据（并行请求，允许局部失败）。"""
        secid = to_secid(code)
        tasks: Dict[str, Any] = {
            "quote": self.quote(secid, with_orderbook=with_orderbook),
            "minute": self.minute(secid, minute_days),
            "kline": self.kline(secid, period, adjust, kline_limit),
        }
        if with_ticks:
            tasks["ticks"] = self.ticks(secid, 60)

        names = list(tasks)
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        payload: Dict[str, Any] = {"secid": secid, "market_status": self.market_status()}
        errors: Dict[str, str] = {}
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                errors[name] = str(res)
                payload[name] = None
            else:
                payload[name] = res
        payload["errors"] = errors
        return payload
