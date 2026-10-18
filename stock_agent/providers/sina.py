"""新浪财经数据源（备用）。

主要价值：
* 实时快照 **含五档盘口**（买卖 1~5 档量价），东方财富 push2 接口不直接提供；
* 东方财富不可用时，可作为实时报价的降级通道。

注意：``hq.sinajs.cn`` 需要携带 ``Referer: https://finance.sina.com.cn``，返回 GBK 文本。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

import httpx

from ..config import Settings, get_settings
from ..utils import chunked, safe_float, split_secid, to_secid
from .base import BaseProvider, DataSourceError

log = logging.getLogger(__name__)

SINA_HQ = "https://hq.sinajs.cn/list="
# 全市场列表（东财限流时的兜底）
SINA_NODE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeData")
# 新浪 K 线（**仅不复权**）：scale=240 为日线，5/15/30/60 为分钟线，最多 1023 根
SINA_KLINE = (
    "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
)
_SINA_SCALE = {
    "1m": 5, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60,
    "daily": 240, "d": 240, "day": 240,
}
_SINA_KLINE_RE = re.compile(r"=\s*\((\[.*\])\)", re.S)

_LINE_RE = re.compile(r'hq_str_([a-z]{2}\d{6})="([^"]*)"')

# hq.sinajs.cn 返回字段顺序
_FIELDS = (
    "name", "open", "pre_close", "price", "high", "low",
    "bid", "ask", "volume", "amount",
    "bid1_volume", "bid1", "bid2_volume", "bid2", "bid3_volume", "bid3",
    "bid4_volume", "bid4", "bid5_volume", "bid5",
    "ask1_volume", "ask1", "ask2_volume", "ask2", "ask3_volume", "ask3",
    "ask4_volume", "ask4", "ask5_volume", "ask5",
    "date", "time", "status",
)

_SIDE_MAP = {"1": "buy", "2": "sell", "4": "neutral"}


def _sina_symbol(secid: str) -> str:
    market, code = split_secid(to_secid(secid))
    return ("sh" if market == 1 else "sz") + code


class SinaProvider(BaseProvider):
    """新浪财经行情（实时快照 + 五档盘口）。"""

    name = "sina"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.http_timeout),
            headers={
                "User-Agent": self.settings.user_agent,
                "Referer": "https://finance.sina.com.cn",
                "Accept": "*/*",
            },
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(self.settings.max_concurrency)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ 原始抓取

    async def _fetch(self, symbols: Sequence[str]) -> str:
        last_err: Optional[Exception] = None
        url = SINA_HQ + ",".join(symbols)
        for attempt in range(self.settings.http_retries + 1):
            try:
                async with self._sem:
                    resp = await self._client.get(url)
                resp.raise_for_status()
                return resp.content.decode("gbk", errors="ignore")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.settings.http_retries:
                    await asyncio.sleep(0.2 * (attempt + 1))
        raise DataSourceError(f"新浪行情请求失败: {last_err}") from last_err

    async def _get_text(self, url: str) -> str:
        last_err: Optional[Exception] = None
        for attempt in range(self.settings.http_retries + 1):
            try:
                async with self._sem:
                    resp = await self._client.get(url)
                resp.raise_for_status()
                return resp.content.decode("utf-8", errors="ignore")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.settings.http_retries:
                    await asyncio.sleep(0.2 * (attempt + 1))
        raise DataSourceError(f"新浪请求失败 {url}: {last_err}") from last_err

    # ------------------------------------------------------------ K 线

    async def kline(
        self,
        secid: str,
        period: str = "daily",
        adjust: str = "none",
        limit: int = 250,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """新浪 K 线。

        .. warning::
           新浪只提供 **不复权**（bfq）数据，请求前/后复权会报错，
           以免与前复权缓存混用。单次最多 1023 根；不支持周/月线。
        """
        if adjust not in ("none", "", None):
            raise DataSourceError("新浪源只提供不复权 K 线（adjust=none）")
        key = (period or "daily").lower()
        scale = _SINA_SCALE.get(key)
        if scale is None:
            raise DataSourceError(f"新浪源不支持周期 {period!r}（支持 5m/15m/30m/60m/daily）")
        sym = _sina_symbol(secid)
        datalen = max(1, min(int(limit), 1023))
        url = (
            SINA_KLINE
            + "?symbol=" + sym
            + f"&scale={scale}&ma=no&datalen={datalen}"
        )
        text = await self._get_text(url)
        m = _SINA_KLINE_RE.search(text)
        if not m:
            raise DataSourceError(f"新浪 K 线返回格式异常: {text[:120]}")
        rows = json.loads(m.group(1))

        bars: List[Dict[str, Any]] = []
        for r in rows:
            raw = str(r.get("day") or "")
            date = raw
            if " " in raw:  # 分钟线 "2026-09-14 15:00:00"
                date = raw[:16]
            bars.append(
                {
                    "date": date,
                    "open": safe_float(r.get("open")),
                    "close": safe_float(r.get("close")),
                    "high": safe_float(r.get("high")),
                    "low": safe_float(r.get("low")),
                    "volume": (safe_float(r.get("volume")) or 0) / 100.0,  # 股 -> 手
                    "amount": None,
                    "amplitude": None,
                    "change_pct": None,
                    "change": None,
                    "turnover_rate": None,
                }
            )
        prev = None
        for b in bars:
            if prev and b["close"] is not None:
                b["change"] = round(b["close"] - prev, 4)
                b["change_pct"] = round((b["close"] - prev) / prev * 100, 2)
            prev = b["close"]

        if start:
            s = str(start).replace("-", "")
            bars = [b for b in bars if b["date"][:10].replace("-", "") >= s]
        if end:
            e = str(end).replace("-", "")
            bars = [b for b in bars if b["date"][:10].replace("-", "") <= e]

        market, code = split_secid(to_secid(secid))
        return {
            "secid": to_secid(secid),
            "code": code,
            "market": market,
            "period": key,
            "adjust": "none",
            "source": "sina",
            "count": len(bars),
            "bars": bars,
        }

    @staticmethod
    def _parse_line(symbol: str, payload: str) -> Optional[Dict[str, Any]]:
        if not payload:
            return None
        parts = payload.split(",")
        if len(parts) < 32:
            return None
        raw = {name: parts[i] for i, name in enumerate(_FIELDS) if i < len(parts)}
        market = 1 if symbol.startswith("sh") else 0
        code = symbol[2:]
        price = safe_float(raw.get("price"))
        pre_close = safe_float(raw.get("pre_close"))
        volume = safe_float(raw.get("volume"))  # 股
        bids, asks = [], []
        for level in range(1, 6):
            # 新浪盘口数量单位为「股」，统一换算成「手」
            bv = safe_float(raw.get(f"bid{level}_volume"))
            bp = safe_float(raw.get(f"bid{level}"))
            av = safe_float(raw.get(f"ask{level}_volume"))
            ap = safe_float(raw.get(f"ask{level}"))
            bids.append({
                "level": level,
                "price": bp if bp else None,
                "volume": round(bv / 100, 2) if bv else None,
            })
            asks.append({
                "level": level,
                "price": ap if ap else None,
                "volume": round(av / 100, 2) if av else None,
            })
        change = None
        change_pct = None
        if price is not None and pre_close:
            change = round(price - pre_close, 4)
            change_pct = round(change / pre_close * 100, 2)
        return {
            "code": code,
            "secid": f"{market}.{code}",
            "market": market,
            "market_name": "SH" if market == 1 else "SZ",
            "name": raw.get("name"),
            "price": price,
            "open": safe_float(raw.get("open")),
            "high": safe_float(raw.get("high")),
            "low": safe_float(raw.get("low")),
            "pre_close": pre_close,
            "change": change,
            "change_pct": change_pct,
            "volume": round(volume / 100, 2) if volume else None,  # 股 -> 手
            "amount": safe_float(raw.get("amount")),
            "date": raw.get("date"),
            "time": raw.get("time"),
            "timestamp": None,
            "orderbook": {"bids": bids, "asks": asks},
            "source": "sina",
        }

    # ------------------------------------------------------------ 对外接口

    async def quotes(self, secids: Sequence[str]) -> List[Dict[str, Any]]:
        if not secids:
            return []
        symbols = [_sina_symbol(s) for s in secids]
        out: List[Dict[str, Any]] = []
        for batch in chunked(symbols, 50):
            text = await self._fetch(batch)
            found = {m.group(1): m.group(2) for m in _LINE_RE.finditer(text)}
            for sym in batch:
                row = self._parse_line(sym, found.get(sym, ""))
                if row:
                    out.append(row)
        return out

    # ------------------------------------------------------------ 全市场列表

    async def all_stocks(
        self, market: str = "all", page_size: int = 100, max_pages: int = 90
    ) -> List[Dict[str, Any]]:
        """拉取全市场快照（新浪分页接口，作为东财的兜底源）。"""
        num = max(20, min(int(page_size), 100))
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for page in range(1, max_pages + 1):
            url = (f"{SINA_NODE}?page={page}&num={num}&sort=symbol&asc=1"
                   f"&node=hs_a&symbol=")
            try:
                text = await self._get_text(url)
            except DataSourceError:
                break
            raw = (text or "").strip()
            if not raw or raw in ("null", "[]"):
                break
            try:
                rows = json.loads(raw)
            except ValueError:
                break
            if not isinstance(rows, list) or not rows:
                break
            for r in rows:
                if not isinstance(r, dict):
                    continue
                code = str(r.get("code") or "")
                sym = str(r.get("symbol") or "")
                if not code or code in seen:
                    continue
                seen.add(code)
                mk = 1 if sym.startswith("sh") else 0
                price = safe_float(r.get("trade"))
                pre = safe_float(r.get("settlement"))
                high, low = safe_float(r.get("high")), safe_float(r.get("low"))
                vol = safe_float(r.get("volume")) or 0.0
                out.append({
                    "code": code,
                    "secid": f"{mk}.{code}",
                    "market": mk,
                    "market_name": "SH" if mk == 1 else "SZ",
                    "name": r.get("name"),
                    "price": price,
                    "change_pct": safe_float(r.get("changepercent")),
                    "change": safe_float(r.get("pricechange")),
                    "volume": vol / 100.0,           # 股 -> 手
                    "amount": safe_float(r.get("amount")),
                    "amplitude": (round((high - low) / pre * 100, 2)
                                  if (high and low and pre) else None),
                    "turnover_rate": safe_float(r.get("turnoverratio")),
                    "pe_dynamic": safe_float(r.get("per")),
                    "volume_ratio": None,
                    "high": high, "low": low,
                    "open": safe_float(r.get("open")),
                    "pre_close": pre,
                    "total_mv": (safe_float(r.get("mktcap")) or 0) * 1e4 or None,
                    "float_mv": (safe_float(r.get("nmc")) or 0) * 1e4 or None,
                    "speed": None, "pb": safe_float(r.get("pb")),
                    "change_60d": None, "change_ytd": None,
                    "main_inflow": None, "pe_ttm": safe_float(r.get("per")),
                    "industry": None, "status": None,
                })
            if len(rows) < num:
                break
        return out

    async def orderbook(self, secid: str) -> Optional[Dict[str, Any]]:
        """仅取五档盘口。"""
        rows = await self.quotes([secid])
        if not rows:
            return None
        row = rows[0]
        return {
            "secid": row["secid"],
            "code": row["code"],
            "name": row["name"],
            "price": row["price"],
            "pre_close": row["pre_close"],
            "date": row["date"],
            "time": row["time"],
            "bids": row["orderbook"]["bids"],
            "asks": row["orderbook"]["asks"],
        }
