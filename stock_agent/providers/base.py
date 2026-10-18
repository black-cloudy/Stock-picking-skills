"""数据源抽象层。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


class Quote(dict):
    """行情字典（保持 dict 以便直接 JSON 序列化）。"""


class DataSourceError(RuntimeError):
    """上游数据源异常。"""


class BaseProvider:
    """行情数据源接口。"""

    name = "base"

    async def close(self) -> None:  # pragma: no cover - 子类实现
        return None

    # -- 实时行情 ---------------------------------------------------------
    async def quotes(self, secids: Sequence[str]) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def quote(self, secid: str) -> Optional[Dict[str, Any]]:
        rows = await self.quotes([secid])
        return rows[0] if rows else None

    # -- 分时 -------------------------------------------------------------
    async def minute(self, secid: str, days: int = 1) -> Dict[str, Any]:
        raise NotImplementedError

    # -- K 线 -------------------------------------------------------------
    async def kline(
        self,
        secid: str,
        period: str = "daily",
        adjust: str = "qfq",
        limit: int = 250,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    # -- 列表 / 搜索 -------------------------------------------------------
    async def stock_list(
        self,
        page: int = 1,
        page_size: int = 50,
        sort_field: str = "f3",
        order: str = "desc",
        market: str = "all",
    ) -> Dict[str, Any]:
        raise NotImplementedError

    async def search(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        raise NotImplementedError
