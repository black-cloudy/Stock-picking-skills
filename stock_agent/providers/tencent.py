"""腾讯财经数据源（备用）。

覆盖能力（与东方财富互补，东方财富被限流时可自动接管）：

* 实时快照 + **五档盘口** —— ``qt.gtimg.cn``
* 分时（当日 / 最近 5 个交易日）—— ``web.ifzq.gtimg.cn/appstock/app/minute|day/query``
* 日/周/月 K 线（支持前复权 / 后复权）—— ``.../fqkline/get``
* 1/5/15/30/60 分钟 K 线 —— ``ifzq.gtimg.cn/appstock/app/kline/mkline``
* 拼音 / 代码搜索 —— ``smartbox.gtimg.cn``

限制：日/周/月 K 单次最多约 800 根，分钟 K 单次最多约 320 根。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

import httpx

from ..config import Settings, get_settings
from ..utils import chunked, safe_float, split_secid, to_secid
from .base import BaseProvider, DataSourceError

log = logging.getLogger(__name__)

QT_URL = "https://qt.gtimg.cn/q="

# 腾讯各接口的备用域名（第一个为主，失败后自动轮换）
# (base_url, path) —— proxy.finance.qq.com 的路径带 /ifzqgtimg 前缀
KLINE_ENDPOINTS = (
    # newfqkline：腾讯目前唯一没被 WAF 拦的日线入口（2026-10 实测）
    ("https://proxy.finance.qq.com", "/ifzqgtimg/appstock/app/newfqkline/get"),
    ("https://web.ifzq.gtimg.cn", "/appstock/app/fqkline/get"),
    ("https://ifzq.gtimg.cn", "/appstock/app/fqkline/get"),
    ("https://proxy.finance.qq.com", "/ifzqgtimg/appstock/app/fqkline/get"),
)
MKLINE_ENDPOINTS = (
    ("https://ifzq.gtimg.cn", "/appstock/app/kline/mkline"),
    ("https://web.ifzq.gtimg.cn", "/appstock/app/kline/mkline"),
    ("https://proxy.finance.qq.com", "/ifzqgtimg/appstock/app/kline/mkline"),
)
MINUTE_ENDPOINTS = (
    ("https://web.ifzq.gtimg.cn", "/appstock/app/minute/query"),
    ("https://ifzq.gtimg.cn", "/appstock/app/minute/query"),
    ("https://proxy.finance.qq.com", "/ifzqgtimg/appstock/app/minute/query"),
)
DAY_ENDPOINTS = (
    ("https://web.ifzq.gtimg.cn", "/appstock/app/day/query"),
    ("https://ifzq.gtimg.cn", "/appstock/app/day/query"),
    ("https://proxy.finance.qq.com", "/ifzqgtimg/appstock/app/day/query"),
)

MAX_FQ_LIMIT = 800    # 日/周/月 K 单次上限
MAX_MK_LIMIT = 320    # 分钟 K 单次上限

# qt.gtimg.cn 返回数组的下标
_QT = {
    "name": 1, "code": 2, "price": 3, "pre_close": 4, "open": 5,
    "volume": 6, "outer": 7, "inner": 8,
    "time": 30, "change": 31, "change_pct": 32, "high": 33, "low": 34,
    "amount_wan": 37, "turnover_rate": 38, "pe_ttm": 39,
    "amplitude": 43, "float_mv_yi": 44, "total_mv_yi": 45, "pb": 46,
    "limit_up": 47, "limit_down": 48, "volume_ratio": 49,
    "bid_diff": 50, "avg_price": 51, "pe_dynamic": 52, "pe_static": 53,
}

_PERIOD_KEY = {"daily": "day", "weekly": "week", "monthly": "month"}
_MK_KEY = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60"}


def qt_symbol(secid: str) -> str:
    """``1.600519`` -> ``sh600519``（北交所 -> ``bj``）。"""
    market, code = split_secid(to_secid(secid))
    if code.startswith(("4", "8", "920")):
        return "bj" + code
    return ("sh" if market == 1 else "sz") + code


class TencentProvider(BaseProvider):
    """腾讯财经行情数据源。"""

    name = "tencent"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.http_timeout),
            headers={
                "User-Agent": self.settings.user_agent,
                "Referer": "https://gu.qq.com/",
                "Accept": "*/*",
            },
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(self.settings.max_concurrency)
        # tag -> [端点索引, 生效时间]
        self._endpoint_state: Dict[str, list] = {}

    #: 多久之后重新尝试首选端点（秒）
    ENDPOINT_RETRY_AFTER = 180.0

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ 基础请求

    async def _get_text(self, url: str, params: Optional[Dict[str, Any]] = None) -> str:
        last_err: Optional[Exception] = None
        for attempt in range(self.settings.http_retries + 1):
            try:
                async with self._sem:
                    resp = await self._client.get(url, params=params)
                resp.raise_for_status()
                return resp.content.decode("gbk", errors="ignore")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.settings.http_retries:
                    await asyncio.sleep(0.2 * (attempt + 1))
        raise DataSourceError(f"腾讯行情请求失败 {url}: {last_err}") from last_err

    async def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        text = await self._get_text(url, params)
        import json

        try:
            return json.loads(text)
        except ValueError as exc:
            raise DataSourceError(f"腾讯返回非 JSON: {text[:120]}") from exc

    async def _get_json_multi(
        self, endpoints: Sequence[tuple], params: Dict[str, Any], tag: str = ""
    ) -> Dict[str, Any]:
        """在多个备用域名间依次尝试（首个可用域名会被记住）。"""
        if not endpoints:
            raise DataSourceError(f"腾讯 {tag} 未配置端点")
        state = self._endpoint_state.setdefault(tag, [0, 0.0])
        now = asyncio.get_running_loop().time()
        if state[0] and (now - state[1]) > self.ENDPOINT_RETRY_AFTER:
            state[0] = 0
        order = list(range(len(endpoints)))[state[0] :] + list(range(len(endpoints)))[: state[0]]

        errors: List[str] = []
        for idx in order:
            base, path = endpoints[idx]
            try:
                payload = await self._get_json(base + path, params)
                if payload.get("code") not in (0, None):
                    raise DataSourceError(f"腾讯返回 code={payload.get('code')} {payload.get('msg')}")
                state[0], state[1] = idx, asyncio.get_running_loop().time()
                return payload
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{base}({type(exc).__name__})")
                # 换下一个域名立即重试
        raise DataSourceError(f"腾讯 {tag} 全部端点失败 -> " + "; ".join(errors))

    # ------------------------------------------------------------ 实时行情

    @staticmethod
    def _parse_qt(symbol: str, payload: str) -> Optional[Dict[str, Any]]:
        parts = payload.split("~")
        if len(parts) < 40:
            return None

        def g(key: str) -> Any:
            idx = _QT.get(key)
            return parts[idx] if idx is not None and idx < len(parts) else None

        market = 1 if symbol.startswith("sh") else 0
        code = parts[2] if len(parts) > 2 else symbol[2:]
        price = safe_float(g("price"))
        pre_close = safe_float(g("pre_close"))
        volume = safe_float(g("volume"))            # 手
        amount = safe_float(g("amount_wan"))
        amount = amount * 1e4 if amount is not None else None   # 万元 -> 元

        # 五档
        def level(price_idx: int, vol_idx: int, lv: int, side: str) -> Dict[str, Any]:
            p = safe_float(parts[price_idx]) if price_idx < len(parts) else None
            v = safe_float(parts[vol_idx]) if vol_idx < len(parts) else None
            return {
                "level": lv,
                "price": p if p else None,
                "volume": v if v else None,
                "side": side,
            }

        bids = [level(9 + i * 2, 10 + i * 2, i + 1, "bid") for i in range(5)]
        asks = [level(19 + i * 2, 20 + i * 2, i + 1, "ask") for i in range(5)]

        raw_time = (g("time") or "").strip()
        ts_str = (
            f"{raw_time[0:4]}-{raw_time[4:6]}-{raw_time[6:8]} "
            f"{raw_time[8:10]}:{raw_time[10:12]}:{raw_time[12:14]}"
            if len(raw_time) >= 14
            else None
        )

        return {
            "code": code,
            "secid": f"{market}.{code}",
            "market": market,
            "market_name": "SH" if market == 1 else ("SZ" if market == 0 else "?"),
            "name": parts[1],
            "price": price,
            "open": safe_float(g("open")),
            "high": safe_float(g("high")),
            "low": safe_float(g("low")),
            "pre_close": pre_close,
            "change": safe_float(g("change")),
            "change_pct": safe_float(g("change_pct")),
            "amplitude": safe_float(g("amplitude")),
            "volume": volume,
            "amount": amount,
            "avg_price": safe_float(g("avg_price")),
            "volume_ratio": safe_float(g("volume_ratio")),
            "limit_up": safe_float(g("limit_up")),
            "limit_down": safe_float(g("limit_down")),
            "outer_volume": safe_float(g("outer")),
            "inner_volume": safe_float(g("inner")),
            "turnover_rate": safe_float(g("turnover_rate")),
            "pe_dynamic": safe_float(g("pe_dynamic")),
            "pe_ttm": safe_float(g("pe_ttm")),
            "pb": safe_float(g("pb")),
            "float_mv": (safe_float(g("float_mv_yi")) or 0) * 1e8 or None,
            "total_mv": (safe_float(g("total_mv_yi")) or 0) * 1e8 or None,
            "bid_diff": safe_float(g("bid_diff")),
            "time": ts_str,
            "timestamp": None,
            "orderbook": {"bids": bids, "asks": asks},
            "source": "tencent",
        }

    async def quotes(self, secids: Sequence[str]) -> List[Dict[str, Any]]:
        if not secids:
            return []
        symbols = [qt_symbol(s) for s in secids]
        out: List[Dict[str, Any]] = []
        for batch in chunked(symbols, 60):
            text = await self._get_text(QT_URL + ",".join(batch))
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("v_") or '="' not in line:
                    continue
                head, _, rest = line.partition('="')
                symbol = head[2:]
                payload = rest.rstrip('";')
                row = self._parse_qt(symbol, payload)
                if row:
                    out.append(row)
        return out

    # ------------------------------------------------------------ 分时

    async def minute(self, secid: str, days: int = 1) -> Dict[str, Any]:
        sym = qt_symbol(secid)
        days = max(1, min(int(days), 5))

        if days <= 1:
            payload = await self._get_json_multi(MINUTE_ENDPOINTS, {"code": sym}, "minute")
            node = (payload.get("data") or {}).get(sym) or {}
            inner = node.get("data") or {}
            day_blocks = [{"date": inner.get("date"), "data": inner.get("data") or []}]
        else:
            payload = await self._get_json_multi(DAY_ENDPOINTS, {"code": sym}, "day")
            node = (payload.get("data") or {}).get(sym) or {}
            day_blocks = node.get("data") or []
            if isinstance(day_blocks, dict):
                day_blocks = [day_blocks]
            day_blocks = list(reversed(day_blocks))[-days:]  # 由旧到新

        qt = node.get("qt") or {}
        qt_row = qt.get(sym) or []
        pre_close = safe_float(qt_row[4]) if len(qt_row) > 4 else None
        name = qt_row[1] if len(qt_row) > 1 else None

        bars: List[Dict[str, Any]] = []
        for block in day_blocks:
            date = str(block.get("date") or "")
            date_fmt = f"{date[0:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date
            prev_cum = 0.0
            for line in block.get("data") or []:
                parts = str(line).split()
                if len(parts) < 3:
                    continue
                hhmm = parts[0]
                price = safe_float(parts[1])
                cum_vol = safe_float(parts[2]) or 0.0
                cum_amt = safe_float(parts[3]) if len(parts) > 3 else None
                vol = max(0.0, cum_vol - prev_cum)
                prev_cum = cum_vol
                avg = (cum_amt / (cum_vol * 100)) if (cum_amt and cum_vol) else None
                bars.append(
                    {
                        "time": f"{date_fmt} {hhmm[:2]}:{hhmm[2:4]}" if len(hhmm) >= 4 else f"{date_fmt} {hhmm}",
                        "open": price,
                        "price": price,
                        "high": price,
                        "low": price,
                        "volume": round(vol, 2),
                        "amount": None,
                        "cum_volume": cum_vol,
                        "avg_price": round(avg, 3) if avg else None,
                        "change_pct": (
                            round((price - pre_close) / pre_close * 100, 2)
                            if price is not None and pre_close
                            else None
                        ),
                    }
                )

        market, code = split_secid(to_secid(secid))
        return {
            "secid": to_secid(secid),
            "code": code,
            "market": market,
            "name": name,
            "pre_close": pre_close,
            "days": days,
            "count": len(bars),
            "source": "tencent",
            "trends": bars,
        }

    # ------------------------------------------------------------ K 线

    async def kline(
        self,
        secid: str,
        period: str = "daily",
        adjust: str = "qfq",
        limit: int = 250,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> Dict[str, Any]:
        sym = qt_symbol(secid)
        key = (period or "daily").lower()
        bars: List[Dict[str, Any]] = []

        if key in _PERIOD_KEY:
            p = _PERIOD_KEY[key]
            fq = "" if adjust in ("none", "", None) else adjust
            want = MAX_FQ_LIMIT if (start or end) else min(max(1, int(limit)), MAX_FQ_LIMIT)
            payload = await self._get_json_multi(
                KLINE_ENDPOINTS, {"param": f"{sym},{p},,,{want},{fq}"}, "fqkline"
            )
            node = (payload.get("data") or {}).get(sym) or {}
            rows: List[Any] = []
            for candidate in (f"{fq}{p}", p, f"qfq{p}", f"hfq{p}"):
                value = node.get(candidate)
                if isinstance(value, list) and value:
                    rows = value
                    break
            for row in rows:
                bars.append(
                    {
                        "date": row[0],
                        "open": safe_float(row[1]),
                        "close": safe_float(row[2]),
                        "high": safe_float(row[3]),
                        "low": safe_float(row[4]),
                        "volume": safe_float(row[5]) if len(row) > 5 else None,
                        "amount": None,
                        "amplitude": None,
                        "change_pct": None,
                        "change": None,
                        "turnover_rate": None,
                    }
                )
        elif key in _MK_KEY:
            p = _MK_KEY[key]
            want = min(max(1, int(limit)), MAX_MK_LIMIT)
            payload = await self._get_json_multi(
                MKLINE_ENDPOINTS, {"param": f"{sym},{p},,{want}"}, "mkline"
            )
            node = (payload.get("data") or {}).get(sym)
            rows = (node or {}).get(p) or []
            for row in rows:
                raw = str(row[0])
                date = (
                    f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}"
                    if len(raw) >= 12
                    else raw
                )
                bars.append(
                    {
                        "date": date,
                        "open": safe_float(row[1]),
                        "close": safe_float(row[2]),
                        "high": safe_float(row[3]),
                        "low": safe_float(row[4]),
                        "volume": safe_float(row[5]) if len(row) > 5 else None,
                        "amount": None,
                        "amplitude": None,
                        "change_pct": None,
                        "change": None,
                        "turnover_rate": None,
                    }
                )
        else:
            raise ValueError(f"腾讯源不支持的周期: {period!r}")

        # 时间范围过滤
        if start:
            s = str(start).replace("-", "")
            bars = [b for b in bars if b["date"].replace("-", "").replace(" ", "").replace(":", "")[:8] >= s]
        if end:
            e = str(end).replace("-", "")
            bars = [b for b in bars if b["date"].replace("-", "").replace(" ", "").replace(":", "")[:8] <= e]

        # 腾讯 fqkline 会多返回 1 根，截断到请求数量
        if limit and len(bars) > int(limit):
            bars = bars[-int(limit):]

        # 补齐涨跌幅（腾讯 K 线不直接返回）
        prev_close = None
        for bar in bars:
            close = bar["close"]
            if prev_close and close is not None:
                bar["change"] = round(close - prev_close, 4)
                bar["change_pct"] = round((close - prev_close) / prev_close * 100, 2)
            prev_close = close

        market, code = split_secid(to_secid(secid))
        return {
            "secid": to_secid(secid),
            "code": code,
            "market": market,
            "period": key,
            "adjust": adjust,
            "count": len(bars),
            "source": "tencent",
            "bars": bars,
        }

    # ------------------------------------------------------------ 五档盘口

    async def orderbook(self, secid: str) -> Optional[Dict[str, Any]]:
        """五档盘口（qt.gtimg.cn 实时快照自带买卖五档）。"""
        rows = await self.quotes([secid])
        if not rows:
            return None
        row = rows[0]
        ts = (row.get("time") or "").strip()
        ob = row.get("orderbook") or {}
        return {
            "secid": row["secid"],
            "code": row["code"],
            "name": row["name"],
            "price": row["price"],
            "pre_close": row["pre_close"],
            "date": ts[:10] if len(ts) >= 10 else None,
            "time": ts[11:] if len(ts) > 10 else None,
            "bids": ob.get("bids") or [],
            "asks": ob.get("asks") or [],
        }

    # ------------------------------------------------------------ 逐笔成交

    TICK_URL = "https://stock.gtimg.cn/data/index.php"
    _SIDE = {"B": "buy", "S": "sell", "M": "neutral"}

    async def ticks(self, secid: str, limit: int = 60) -> List[Dict[str, Any]]:
        """最近逐笔成交（腾讯明细分页接口，每页约 70 条）。

        该接口没有「最后一页」参数，用指数探测 + 二分定位末页，再取尾部。
        """
        market, code = split_secid(to_secid(secid))
        sym = ("sh" if market == 1 else "sz") + code
        limit = max(1, min(int(limit), 500))

        async def page(p: int) -> List[str]:
            try:
                text = await self._get_text(
                    self.TICK_URL,
                    {"appn": "detail", "action": "data", "c": sym, "p": p},
                )
            except DataSourceError:
                return []
            m = re.search(r"=\[(.*)\];?", text, re.S)
            if not m:
                return []
            return [x for x in m.group(1).split("|") if x.count("/") >= 6]

        if not await page(0):
            return []

        # 指数探测上界，再用二分找最后一个非空页
        lo, hi = 0, 1
        while hi <= 256 and await page(hi):
            lo, hi = hi, hi * 2
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if await page(mid):
                lo = mid
            else:
                hi = mid

        rows: List[str] = []
        # 每页约 70 条，按需多取几页以凑够 limit
        need = max(2, (limit + 69) // 70 + 1)
        for p in range(max(0, lo - need + 1), lo + 1):
            rows.extend(await page(p))

        out: List[Dict[str, Any]] = []
        for rec in rows:
            parts = rec.split("/")
            if len(parts) < 7:
                continue
            # 格式：序号/时间/价格/涨跌/成交量(手)/成交额(元)/方向
            out.append({
                "time": parts[1],
                "price": safe_float(parts[2]),
                "volume": safe_float(parts[4]),
                "amount": safe_float(parts[5]),
                "trades": None,
                "side": self._SIDE.get(parts[6].strip(), "neutral"),
            })
        return out[-limit:]

    # ------------------------------------------------------------ 搜索

    async def search(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        text = await self._get_text(
            SEARCH_URL, {"v": 2, "q": keyword, "t": "all"}
        )
        _, _, payload = text.partition('="')
        payload = payload.rstrip('";\n')
        if not payload or payload == "N":
            return []
        out: List[Dict[str, Any]] = []
        for item in payload.split("^"):
            fields = item.split("~")
            if len(fields) < 3:
                continue
            market_tag, code, name = fields[0], fields[1], fields[2]
            market = 1 if market_tag == "sh" else 0
            if not code.isdigit():
                continue
            out.append(
                {
                    "code": code,
                    "name": name,
                    "secid": f"{market}.{code}",
                    "market": market,
                    "type": fields[4] if len(fields) > 4 else None,
                    "pinyin": fields[3] if len(fields) > 3 else None,
                }
            )
            if len(out) >= limit:
                break
        return out
