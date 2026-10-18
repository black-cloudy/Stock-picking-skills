"""轻量级 TTL 缓存（带防击穿的单飞锁）。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Tuple


class TTLCache:
    """线程/协程安全的进程内缓存。"""

    def __init__(self) -> None:
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------ 基础操作

    def get(self, key: str, default: Any = None) -> Any:
        item = self._store.get(key)
        if item is None:
            return default
        expire_at, value = item
        if expire_at < time.monotonic():
            self._store.pop(key, None)
            return default
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        self._store[key] = (time.monotonic() + ttl, value)

    def invalidate(self, prefix: str = "") -> int:
        if not prefix:
            n = len(self._store)
            self._store.clear()
            return n
        keys = [k for k in self._store if k.startswith(prefix)]
        for k in keys:
            self._store.pop(k, None)
        return len(keys)

    @property
    def size(self) -> int:
        return len(self._store)

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "size": self.size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }

    # ------------------------------------------------------------ 单飞

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get_or_set(
        self,
        key: str,
        ttl: float,
        factory: Callable[[], Awaitable[Any]],
        *,
        force: bool = False,
    ) -> Any:
        """命中则直接返回；未命中则调用 ``factory`` 并写入缓存。

        同一 key 的并发请求只会真正执行一次 ``factory``（single-flight）。
        """
        if not force:
            cached = self.get(key)
            if cached is not None:
                self.hits += 1
                return cached
        self.misses += 1

        lock = await self._lock_for(key)
        async with lock:
            if not force:
                cached = self.get(key)
                if cached is not None:
                    return cached
            value = await factory()
            if value is not None:
                self.set(key, value, ttl)
            return value


_default_cache = TTLCache()


def get_cache() -> TTLCache:
    return _default_cache
