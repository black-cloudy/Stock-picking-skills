"""东方财富数据源。

使用公开的 push2 / push2his 接口，无需注册 token，覆盖：

* 实时行情（含批量、五档以外的全字段）
* 分时图（当日 / 多日）
* K 线（1/5/15/30/60 分钟、日、周、月，支持不复权/前复权/后复权）
* 全市场股票列表、涨跌幅榜
* 关键词搜索（代码 / 名称 / 拼音首字母）
* 逐笔成交明细

接口速查
--------
``push2.eastmoney.com/api/qt/stock/get``        单只行情
``push2.eastmoney.com/api/qt/ulist.np/get``     批量行情
``push2.eastmoney.com/api/qt/clist/get``        列表 / 排行
``push2his.eastmoney.com/api/qt/stock/trends2/get``  分时
``push2his.eastmoney.com/api/qt/stock/kline/get``    K 线
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

import httpx

from ..config import Settings, get_settings
from ..utils import (
    chunked,
    safe_float,
    safe_int,
    split_secid,
    to_secid,
)
from .base import BaseProvider, DataSourceError

log = logging.getLogger(__name__)

PUSH2 = "https://push2.eastmoney.com"
PUSH2HIS = "https://push2his.eastmoney.com"
PUSH2EX = "https://push2ex.eastmoney.com"

# 涨停板专题接口（push2ex）
_POOL_PATHS = {
    "zt": "/getTopicZTPool",   # 涨停池
    "zb": "/getTopicZBPool",   # 炸板池
    "dt": "/getTopicDTPool",   # 跌停池
    "qs": "/getTopicQSPool",   # 强势股池
    "cx": "/getTopicCXPool",   # 昨日涨停今日表现（连板潜力）
}
_POOL_TOKEN = "7eea3edcaed734bea9cbfc24409ed989"

# K 线周期 -> 东方财富 klt
PERIOD_KLT: Dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "daily": 101,
    "weekly": 102,
    "monthly": 103,
}
# 复权方式 -> fqt
ADJUST_FQT: Dict[str, int] = {
    "none": 0,
    "": 0,
    "qfq": 1,   # 前复权
    "hfq": 2,   # 后复权
}

# 市场筛选（fs 参数）
MARKET_FS: Dict[str, str] = {
    "all": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
    "sh": "m:1+t:2,m:1+t:23",
    "sz": "m:0+t:6,m:0+t:80",
    "bj": "m:0+t:81+s:2048",
    "cyb": "m:0+t:80",           # 创业板
    "kcb": "m:1+t:23",           # 科创板
    "main": "m:0+t:6,m:1+t:2",   # 沪深主板
    "index": "m:1+s:2,m:0+t:5",  # 指数
    "etf": "m:0+t:21,m:1+t:22",  # ETF
}

# 排行榜排序字段
SORT_FIELDS = {
    "pct": "f3",          # 涨跌幅
    "price": "f2",        # 最新价
    "change": "f4",       # 涨跌额
    "volume": "f5",       # 成交量
    "amount": "f6",       # 成交额
    "amplitude": "f7",    # 振幅
    "turnover": "f8",     # 换手率
    "pe": "f9",           # 市盈率(动)
    "volume_ratio": "f10",  # 量比
    "total_mv": "f20",    # 总市值
    "float_mv": "f21",    # 流通市值
    "pb": "f23",          # 市净率
    "main_inflow": "f62",  # 主力净流入
    "speed": "f22",       # 涨速
    "change_60d": "f24",  # 60日涨跌幅
    "change_ytd": "f25",  # 年初至今
    "code": "f12",
}

# 单只行情全字段
_QUOTE_FIELDS = (
    "f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f57,f58,f59,f60,f62,f71,f84,f85,"
    "f86,f92,f107,f116,f117,f127,f128,f136,f140,f141,f152,f162,f167,f168,f169,"
    "f170,f171,f177,f191,f192,f292"
)

# 批量行情字段
_LIST_FIELDS = (
    "f12,f13,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f15,f16,f17,f18,f20,f21,f22,f23,"
    "f24,f25,f62,f115,f128,f136,f140,f141,f152"
)


def _prev_date(date_str: str) -> str:
    """``20240102`` -> ``20240101``（估算上一天，仅用于 K 线翻页）。"""
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        return (dt - timedelta(days=1)).strftime("%Y%m%d")
    except ValueError:
        return date_str


class EastMoneyProvider(BaseProvider):
    """东方财富行情数据源。"""

    name = "eastmoney"

    #: 每个上游主机可用的备用镜像。主站被限流时自动切换。
    HOST_POOLS: Dict[str, List[str]] = {
        "push2.eastmoney.com": [
            "https://push2.eastmoney.com",
            "https://push2delay.eastmoney.com",
        ],
        "push2his.eastmoney.com": [
            "https://push2his.eastmoney.com",
            "https://push2delay.eastmoney.com",
        ],
    }

    #: 多久之后重新尝试首选主机（秒）
    HOST_RETRY_AFTER = 180.0

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.http_timeout),
            headers={
                "User-Agent": self.settings.user_agent,
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "*/*",
            },
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(self.settings.max_concurrency)
        # netloc -> (当前索引, 该索引生效时间)
        self._host_state: Dict[str, tuple] = {}
        # netloc -> 最近一次实际使用的主机（用于标记数据是否延时）
        self._used_host: Dict[str, str] = {}
        self._host_lock = asyncio.Lock()

    @property
    def delayed(self) -> bool:
        """当且仅当最近一次行情请求落在了延时镜像上。"""
        return "push2delay" in self._used_host.get("push2.eastmoney.com", "")

    # ------------------------------------------------------------ 基础请求

    async def close(self) -> None:
        await self._client.aclose()

    async def _once(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        async with self._sem:
            resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _host_candidates(self, netloc: str) -> List[str]:
        pool = self.HOST_POOLS.get(netloc)
        if not pool:
            return []
        idx, stamp = self._host_state.get(netloc, (0, 0.0))
        # 距离上次切换已久，重置回首选主机
        if idx and (asyncio.get_running_loop().time() - stamp) > self.HOST_RETRY_AFTER:
            idx = 0
        self._host_state[netloc] = (idx, stamp)
        return pool[idx:] + pool[:idx]

    async def _get_json(
        self, url: str, params: Dict[str, Any], allow_mirror: bool = True
    ) -> Dict[str, Any]:
        """请求 push2 系列接口。

        ``allow_mirror=False`` 时只使用首选主机（实时行情），避免静默落到
        15 分钟延时镜像 ``push2delay`` 上。
        """
        parsed = urlsplit(url)
        path = parsed.path
        candidates = self._host_candidates(parsed.netloc)
        if not candidates:
            candidates = [f"{parsed.scheme}://{parsed.netloc}"]
        if not allow_mirror:
            candidates = candidates[:1]

        errors: List[str] = []
        for base in candidates:
            for attempt in range(self.settings.http_retries + 1):
                try:
                    data = await self._once(base + path, params)
                    pool = self.HOST_POOLS.get(parsed.netloc, [])
                    if base in pool:
                        self._host_state[parsed.netloc] = (
                            pool.index(base), asyncio.get_running_loop().time()
                        )
                    self._used_host[parsed.netloc] = base
                    return data
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{base}: {type(exc).__name__}")
                    if attempt < self.settings.http_retries:
                        await asyncio.sleep(0.2 * (attempt + 1))
        raise DataSourceError(f"请求 {path} 失败 -> " + "; ".join(errors))

    @staticmethod
    def _require_data(payload: Dict[str, Any], what: str) -> Dict[str, Any]:
        data = payload.get("data")
        if not data:
            raise DataSourceError(f"{what} 无数据（代码可能不存在或已退市）")
        return data

    # ------------------------------------------------------------ 行情解析

    @staticmethod
    def _parse_list_row(row: Dict[str, Any]) -> Dict[str, Any]:
        code = str(row.get("f12") or "")
        market = safe_int(row.get("f13"), 0) or 0
        return {
            "code": code,
            "secid": f"{market}.{code}",
            "market": market,
            "market_name": "SH" if market == 1 else ("SZ" if market == 0 else "?"),
            "name": row.get("f14"),
            "price": safe_float(row.get("f2")),
            "change_pct": safe_float(row.get("f3")),
            "change": safe_float(row.get("f4")),
            "volume": safe_float(row.get("f5")),          # 手
            "amount": safe_float(row.get("f6")),          # 元
            "amplitude": safe_float(row.get("f7")),       # 振幅 %
            "turnover_rate": safe_float(row.get("f8")),   # 换手率 %
            "pe_dynamic": safe_float(row.get("f9")),
            "volume_ratio": safe_float(row.get("f10")),
            "high": safe_float(row.get("f15")),
            "low": safe_float(row.get("f16")),
            "open": safe_float(row.get("f17")),
            "pre_close": safe_float(row.get("f18")),
            "total_mv": safe_float(row.get("f20")),
            "float_mv": safe_float(row.get("f21")),
            "speed": safe_float(row.get("f22")),
            "pb": safe_float(row.get("f23")),
            "change_60d": safe_float(row.get("f24")),
            "change_ytd": safe_float(row.get("f25")),
            "main_inflow": safe_float(row.get("f62")),
            "pe_ttm": safe_float(row.get("f115")),
            "industry": row.get("f128") if isinstance(row.get("f128"), str) else None,
            "status": safe_int(row.get("f152")),
        }

    # ------------------------------------------------------------ 实时行情

    async def quotes(self, secids: Sequence[str]) -> List[Dict[str, Any]]:
        """批量获取实时行情，自动分批（每批 100 只）。"""
        if not secids:
            return []
        normalized = [to_secid(s) for s in secids]
        out: List[Dict[str, Any]] = []
        for batch in chunked(normalized, 100):
            payload = await self._get_json(
                f"{PUSH2}/api/qt/ulist.np/get",
                {
                    "fltt": 2,
                    "invt": 2,
                    "secids": ",".join(batch),
                    "fields": _LIST_FIELDS,
                    "_": int(datetime.now().timestamp() * 1000),
                },
                allow_mirror=False,
            )
            data = payload.get("data") or {}
            for row in data.get("diff") or []:
                if isinstance(row, dict):
                    out.append(self._parse_list_row(row))
        return out

    async def quote(self, secid: str) -> Optional[Dict[str, Any]]:
        """单只行情，字段更全（含均价、涨停价、委比、每股净资产等）。"""
        secid = to_secid(secid)
        payload = await self._get_json(
            f"{PUSH2}/api/qt/stock/get",
            {
                "secid": secid,
                "fltt": 2,
                "invt": 2,
                "fields": _QUOTE_FIELDS,
                "_": int(datetime.now().timestamp() * 1000),
            },
            allow_mirror=False,
        )
        data = payload.get("data")
        if not data:
            return None
        code = str(data.get("f57") or "")
        market, _ = split_secid(secid)
        price = safe_float(data.get("f43"))
        pre_close = safe_float(data.get("f60"))
        ts = safe_int(data.get("f86"))
        return {
            "code": code,
            "secid": secid,
            "market": market,
            "market_name": "SH" if market == 1 else ("SZ" if market == 0 else "?"),
            "name": data.get("f58"),
            "price": price,
            "open": safe_float(data.get("f46")),
            "high": safe_float(data.get("f44")),
            "low": safe_float(data.get("f45")),
            "pre_close": pre_close,
            "change": safe_float(data.get("f169")),
            "change_pct": safe_float(data.get("f170")),
            "amplitude": safe_float(data.get("f171")),
            "volume": safe_float(data.get("f47")),      # 手
            "amount": safe_float(data.get("f48")),      # 元
            "avg_price": safe_float(data.get("f71")),
            "volume_ratio": safe_float(data.get("f50")),
            "limit_up": safe_float(data.get("f51")),
            "limit_down": safe_float(data.get("f52")),
            "outer_volume": safe_float(data.get("f49")),
            "turnover_rate": safe_float(data.get("f168")),
            "pe_dynamic": safe_float(data.get("f162")),
            "pb": safe_float(data.get("f167")),
            "total_share": safe_float(data.get("f84")),
            "float_share": safe_float(data.get("f85")),
            "total_mv": safe_float(data.get("f116")),
            "float_mv": safe_float(data.get("f117")),
            "eps": safe_float(data.get("f92")),          # 每股净资产
            "industry": data.get("f127"),
            "main_inflow": safe_float(data.get("f62")),
            "bid_ratio": safe_float(data.get("f191")),   # 委比
            "bid_diff": safe_float(data.get("f192")),    # 委差
            "status": safe_int(data.get("f292")),
            "decimal": safe_int(data.get("f59"), 2) or 2,
            "timestamp": ts,
            "time": (
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None
            ),
        }

    # ------------------------------------------------------------ 分时图

    async def minute(self, secid: str, days: int = 1) -> Dict[str, Any]:
        """分时数据。``days`` 取值 1~5（5 即"5 日分时"）。"""
        secid = to_secid(secid)
        days = max(1, min(int(days), 5))
        payload = await self._get_json(
            f"{PUSH2HIS}/api/qt/stock/trends2/get",
            {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "iscr": 0,
                "ndays": days,
                "_": int(datetime.now().timestamp() * 1000),
            },
            allow_mirror=False,
        )
        data = self._require_data(payload, "分时数据")
        pre_close = safe_float(data.get("preClose")) or safe_float(data.get("prePrice")) or 0.0
        bars: List[Dict[str, Any]] = []
        for line in data.get("trends") or []:
            parts = str(line).split(",")
            if len(parts) < 8:
                continue
            price = safe_float(parts[2])
            avg = safe_float(parts[7])
            bars.append(
                {
                    "time": parts[0],
                    "open": safe_float(parts[1]),
                    "price": price,
                    "high": safe_float(parts[3]),
                    "low": safe_float(parts[4]),
                    "volume": safe_float(parts[5]),   # 手
                    "amount": safe_float(parts[6]),   # 元
                    "avg_price": avg,
                    "change_pct": (
                        round((price - pre_close) / pre_close * 100, 2)
                        if price is not None and pre_close
                        else None
                    ),
                }
            )
        market, _ = split_secid(secid)
        return {
            "secid": secid,
            "code": str(data.get("code") or ""),
            "market": market,
            "name": data.get("name"),
            "pre_close": pre_close,
            "days": days,
            "count": len(bars),
            "trends": bars,
        }

    # ------------------------------------------------------------ K 线

    async def _kline_once(
        self,
        secid: str,
        klt: int,
        fqt: int,
        limit: int,
        end: str,
        begin: str = "0",
    ) -> List[Dict[str, Any]]:
        payload = await self._get_json(
            f"{PUSH2HIS}/api/qt/stock/kline/get",
            {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": klt,
                "fqt": fqt,
                "beg": begin,
                "end": end,
                "lmt": limit,
                "_": int(datetime.now().timestamp() * 1000),
            },
            allow_mirror=False,  # 延缓镜像不提供 K 线，直接走备用源
        )
        data = payload.get("data") or {}
        out: List[Dict[str, Any]] = []
        for line in data.get("klines") or []:
            parts = str(line).split(",")
            if len(parts) < 7:
                continue
            out.append(
                {
                    "date": parts[0],
                    "open": safe_float(parts[1]),
                    "close": safe_float(parts[2]),
                    "high": safe_float(parts[3]),
                    "low": safe_float(parts[4]),
                    "volume": safe_float(parts[5]),                 # 手
                    "amount": safe_float(parts[6]),                 # 元
                    "amplitude": safe_float(parts[7]) if len(parts) > 7 else None,
                    "change_pct": safe_float(parts[8]) if len(parts) > 8 else None,
                    "change": safe_float(parts[9]) if len(parts) > 9 else None,
                    "turnover_rate": safe_float(parts[10]) if len(parts) > 10 else None,
                }
            )
        return out

    async def kline(
        self,
        secid: str,
        period: str = "daily",
        adjust: str = "qfq",
        limit: int = 250,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取 K 线。

        Parameters
        ----------
        period: ``1m`` ``5m`` ``15m`` ``30m`` ``60m`` ``daily`` ``weekly`` ``monthly``
        adjust: ``none`` ``qfq``(默认) ``hfq``
        limit : 返回的最大条数（日/周/月会自动翻页，最多 5000 条）
        start / end: ``YYYYMMDD``，可选
        """
        key = (period or "daily").lower()
        if key not in PERIOD_KLT:
            raise ValueError(f"不支持的周期: {period!r}，可选: {list(PERIOD_KLT)}")
        if adjust not in ADJUST_FQT:
            raise ValueError(f"不支持的复权方式: {adjust!r}，可选: {list(ADJUST_FQT)}")

        secid = to_secid(secid)
        klt = PERIOD_KLT[key]
        fqt = ADJUST_FQT[adjust]
        limit = max(1, min(int(limit), 5000))
        end_param = (end or "20500101").replace("-", "")
        begin_param = (start or "0").replace("-", "")

        collected: List[Dict[str, Any]] = []
        remaining = limit
        page_size = 1000

        while remaining > 0:
            batch = min(remaining, page_size)
            rows = await self._kline_once(secid, klt, fqt, batch, end_param, begin_param)
            if not rows:
                break
            collected = rows + collected
            remaining -= len(rows)
            if len(rows) < batch:
                break
            if klt < 101 or begin_param != "0":
                # 分钟线不做翻页（避免跨日重复）；指定了 begin 时由上游截断
                break
            end_param = _prev_date(rows[0]["date"].replace("-", ""))
            if end_param <= "19900101":
                break

        # 去重（翻页边界可能重复）并保持时间升序
        dedup: Dict[str, Dict[str, Any]] = {}
        for bar in collected:
            dedup[bar["date"]] = bar
        bars = [dedup[k] for k in sorted(dedup.keys())][-limit:]

        market, _ = split_secid(secid)
        return {
            "secid": secid,
            "code": split_secid(secid)[1],
            "market": market,
            "period": key,
            "adjust": adjust,
            "count": len(bars),
            "bars": bars,
        }

    # ------------------------------------------------------------ 列表 / 排行

    async def stock_list(
        self,
        page: int = 1,
        page_size: int = 50,
        sort_field: str = "pct",
        order: str = "desc",
        market: str = "all",
    ) -> Dict[str, Any]:
        """分页获取股票列表 / 排行榜。"""
        fs = MARKET_FS.get(market, MARKET_FS["all"])
        fid = SORT_FIELDS.get(sort_field, sort_field)
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        payload = await self._get_json(
            f"{PUSH2}/api/qt/clist/get",
            {
                "pn": page,
                "pz": page_size,
                "po": 1 if order == "desc" else 0,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "dect": 1,
                "fid": fid,
                "fs": fs,
                "fields": _LIST_FIELDS,
                "_": int(datetime.now().timestamp() * 1000),
            },
        )
        data = payload.get("data") or {}
        rows = [self._parse_list_row(r) for r in (data.get("diff") or []) if isinstance(r, dict)]
        total = safe_int(data.get("total"), 0) or 0
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if page_size else 0,
            "market": market,
            "sort_field": sort_field,
            "order": order,
            "delayed": self.delayed,
            "source": "eastmoney" + ("(延时)" if self.delayed else ""),
            "items": rows,
        }

    # ------------------------------------------------------------ 涨停板专题

    async def limit_up_pool(
        self, date: str, kind: str = "zt", page_size: int = 600
    ) -> Dict[str, Any]:
        """拉取指定日期的涨停板专题数据。

        Parameters
        ----------
        date: ``YYYYMMDD``
        kind: ``zt`` 涨停池 / ``zb`` 炸板池 / ``dt`` 跌停池 / ``qs`` 强势股池

        Returns
        -------
        ``{"date": 实际交易日, "total": n, "items": [...]}``；
        非交易日或数据缺失时 ``items`` 为空。
        """
        path = _POOL_PATHS.get(kind)
        if path is None:
            raise ValueError(f"不支持的涨停板类型: {kind!r}，可选 {list(_POOL_PATHS)}")
        date = str(date).replace("-", "")
        payload = await self._get_json(
            f"{PUSH2EX}{path}",
            {
                "ut": _POOL_TOKEN,
                "dpt": "wz.ztzt",
                "Pageindex": 0,
                "pagesize": page_size,
                "sort": "fbt:asc",
                "date": date,
                "_": int(datetime.now().timestamp() * 1000),
            },
        )
        data = payload.get("data") or {}
        qdate = data.get("qdate")
        items: List[Dict[str, Any]] = []
        for row in data.get("pool") or []:
            code = str(row.get("c") or "")
            if not code:
                continue
            market = safe_int(row.get("m"), 0) or 0
            price = safe_float(row.get("p"))
            items.append(
                {
                    "code": code,
                    "secid": f"{market}.{code}",
                    "market": market,
                    "name": row.get("n"),
                    # push2ex 的价格是 ×1000 的整数
                    "price": round(price / 1000.0, 3) if price is not None else None,
                    "change_pct": safe_float(row.get("zdp")),
                    "amount": safe_float(row.get("amount")),
                    "float_mv": safe_float(row.get("ltsz")),
                    "total_mv": safe_float(row.get("tshare")),
                    "turnover_rate": safe_float(row.get("hs")),
                    "boards": safe_int(row.get("lbc"), 1),          # 连板数
                    "first_seal": safe_int(row.get("fbt")),        # 首次封板 HHMMSS
                    "last_seal": safe_int(row.get("lbt")),
                    "seal_fund": safe_float(row.get("fund")),       # 封板资金
                    "broken": safe_int(row.get("zbc"), 0),         # 炸板次数
                    "industry": row.get("hybk"),
                    "stat_days": (row.get("zttj") or {}).get("days"),
                    "stat_count": (row.get("zttj") or {}).get("ct"),
                }
            )
        return {
            "date": str(qdate) if qdate else None,
            "requested": date,
            "kind": kind,
            "total": safe_int(data.get("tc"), len(items)) or len(items),
            "count": len(items),
            "items": items,
        }

    # ------------------------------------------------------------ 搜索

    async def all_stocks(
        self, market: str = "all", page_size: int = 100, max_pages: int = 120
    ) -> List[Dict[str, Any]]:
        """拉取整个市场快照（自动翻页，每页 100 条）。"""
        fs = MARKET_FS.get(market, MARKET_FS["all"])
        page_size = max(20, min(int(page_size), 100))  # 上游硬上限 100
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for page in range(1, max_pages + 1):
            payload = await self._get_json(
                f"{PUSH2}/api/qt/clist/get",
                {
                    "pn": page,
                    "pz": page_size,
                    "po": 1,
                    "np": 1,
                    "fltt": 2,
                    "invt": 2,
                    "dect": 1,
                    "fid": "f12",
                    "fs": fs,
                    "fields": _LIST_FIELDS,
                    "_": int(datetime.now().timestamp() * 1000),
                },
            )
            data = payload.get("data") or {}
            rows = [r for r in (data.get("diff") or []) if isinstance(r, dict)]
            if not rows:
                break
            for row in rows:
                parsed = self._parse_list_row(row)
                if parsed["code"] and parsed["secid"] not in seen:
                    seen.add(parsed["secid"])
                    out.append(parsed)
            total = safe_int(data.get("total"), 0) or 0
            if len(out) >= total or len(rows) < page_size:
                break
        return out

    async def search(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        """按代码 / 名称 / 拼音搜索证券。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        limit = max(1, min(int(limit), 50))

        # 主接口
        try:
            payload = await self._get_json(
                "https://search-codetable.eastmoney.com/codetable/search/web",
                {"client": "web", "keyword": keyword, "pageIndex": 1, "pageSize": limit},
            )
            rows = payload.get("result") or []
            if rows:
                return [
                    {
                        "code": r.get("code"),
                        "name": r.get("shortName"),
                        "secid": f"{safe_int(r.get('market'), 0)}.{r.get('code')}",
                        "market": safe_int(r.get("market"), 0),
                        "type": r.get("securityTypeName"),
                        "pinyin": r.get("pinyin"),
                    }
                    for r in rows
                    if r.get("code")
                ]
        except DataSourceError:
            pass

        # 备用接口
        payload = await self._get_json(
            "https://searchapi.eastmoney.com/api/suggest/get",
            {
                "input": keyword,
                "type": 14,
                "token": "D43BF722C8E33BDC906FB84D85E326E8",
                "count": limit,
            },
        )
        data = (payload.get("QuotationCodeTable") or {}).get("Data") or []
        return [
            {
                "code": r.get("Code"),
                "name": r.get("Name"),
                "secid": r.get("QuoteID") or f"{r.get('MktNum', 0)}.{r.get('Code')}",
                "market": safe_int(r.get("MktNum"), 0),
                "type": r.get("SecurityTypeName"),
                "pinyin": r.get("PinYin"),
            }
            for r in data
            if r.get("Code")
        ]

    # ------------------------------------------------------------ 逐笔成交

    async def ticks(self, secid: str, limit: int = 60) -> List[Dict[str, Any]]:
        """最近逐笔成交明细。"""
        secid = to_secid(secid)
        payload = await self._get_json(
            f"{PUSH2}/api/qt/stock/details/get",
            {
                "secid": secid,
                "fields1": "f1,f2,f3,f4",
                "fields2": "f51,f52,f53,f54,f55",
                "pos": -abs(int(limit)),
                "_": int(datetime.now().timestamp() * 1000),
            },
        )
        data = payload.get("data") or {}
        # 行格式: 时间,价格,成交量(手),成交笔数,买卖方向(1买 2卖 4中性)
        side_label = {"1": "buy", "2": "sell", "4": "neutral"}
        out: List[Dict[str, Any]] = []
        for line in data.get("details") or []:
            parts = str(line).split(",")
            if len(parts) < 3:
                continue
            price = safe_float(parts[1])
            volume = safe_float(parts[2])
            out.append(
                {
                    "time": parts[0],
                    "price": price,
                    "volume": volume,               # 手
                    "amount": (price * volume * 100) if price and volume else None,
                    "trades": safe_float(parts[3]) if len(parts) > 3 else None,
                    "side": side_label.get(parts[4].strip(), "neutral") if len(parts) > 4 else "neutral",
                }
            )
        return out
