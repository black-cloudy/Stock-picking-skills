"""扫描调度器：候选池 → 并发取数 → 策略判定 → 打分排序。"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..config import Settings, get_settings
from ..service import MarketService
from ..utils import now_cn
from .indicators import is_st
from .store import ScreenerStore
from .strategies import (
    MeiRenJianConfig,
    SteadyClimbConfig,
    detect_meirenjian,
    detect_steady_climb,
)
from .patterns import SecondBoardConfig, detect_second_board, score_to_probability, structure_reasons
from .trend import (
    SteadyTrendConfig,
    detect_steady_trend,
    quick_trend_ok,
)
from .lowpos import (
    LowPosConfig,
    detect_low_position,
)

log = logging.getLogger(__name__)

# 指数代码，用来取精确的交易日历
_CALENDAR_INDEX = "1.000001"


@dataclass
class ScanProgress:
    """扫描进度。"""

    stage: str = ""
    total: int = 0
    done: int = 0
    failed: int = 0
    cached: int = 0
    started: float = field(default_factory=time.monotonic)
    callback: Optional[Callable[["ScanProgress"], None]] = None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def speed(self) -> float:
        return self.done / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def eta(self) -> float:
        if self.speed <= 0:
            return 0.0
        return max(0.0, (self.total - self.done) / self.speed)

    def tick(self, ok: bool = True, n: int = 1) -> None:
        self.done += n
        if not ok:
            self.failed += n
        if self.callback:
            self.callback(self)

    def line(self) -> str:
        pct = (self.done / self.total * 100) if self.total else 100.0
        bar_len = 26
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        return (
            f"\r  {self.stage} [{bar}] {pct:5.1f}%  "
            f"{self.done}/{self.total}  失败{self.failed}  "
            f"{self.speed:5.1f}/s  ETA {self.eta:4.0f}s"
        )


class ConsoleProgress:
    """把进度打印到 stderr（终端一行刷新）。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._last = 0.0

    def __call__(self, p: ScanProgress) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if p.done < p.total and (now - self._last) < 0.08:
            return
        self._last = now
        sys.stderr.write(p.line())
        sys.stderr.flush()
        if p.done >= p.total:
            sys.stderr.write("\n")


def default_store_path(settings: Optional[Settings] = None) -> str:
    settings = settings or get_settings()
    return f"{settings.data_dir}/screener.db"


class Screener:
    """选股调度器。"""

    def __init__(
        self,
        service: Optional[MarketService] = None,
        store: Optional[ScreenerStore] = None,
        settings: Optional[Settings] = None,
        concurrency: int = 24,
        progress: Optional[Callable[[ScanProgress], None]] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.svc = service or MarketService(self.settings)
        self._owns_service = service is None
        self.store = store or ScreenerStore(default_store_path(self.settings))
        self.concurrency = max(1, concurrency)
        self.progress_cb = progress
        self._universe_cache: Optional[List[Dict[str, Any]]] = None
        self._names: Dict[str, str] = {}

    # ------------------------------------------------------------ 生命周期

    async def __aenter__(self) -> "Screener":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_service:
            await self.svc.close()

    def _shares(self, code: str, adjust: str = "qfq") -> float:
        """流通股本（用于换手率 / 市值特征），带内存缓存。"""
        cache = getattr(self, "_share_cache", None)
        if cache is None:
            cache = self._share_cache = {c: b.get("float_shares", 0.0)
                                         for c, b in (getattr(self, "_meta_map", {}) or {}).items()}
        return cache.get(code, 0.0)

    # ------------------------------------------------------------ 基础数据

    def suspended_lag(
        self, bars_map: Dict[str, List[Dict[str, Any]]], adjust: str = "qfq", window: int = 30
    ) -> Dict[str, int]:
        """返回 ``{code: 停牌天数}``（只用最近 window 个交易日做参照）。

        停牌股的最后根本线会落后于市场最新交易日；这类票不可交易，
        而且形态是“冻结”的，没参考价值。

        注意：基准日取「**大多数股票都有的最新日期**」，而不是直接取最大日期。
        否则当缓存处于增量刷新中（一部分股票有 T 日、一部分只有 T-1 日）时，
        会把 T-1 的股票全部误判成停牌。
        """
        dates = self.store.recent_dates(adjust, window)
        if not dates:
            return {}
        # 统计各股票末根日期，找到“多数派”作为基准日
        counts: Dict[str, int] = {}
        for bars in bars_map.values():
            if bars:
                d = bars[-1]["date"]
                counts[d] = counts.get(d, 0) + 1
        if not counts:
            return {}
        total = sum(counts.values())
        ref = None
        for d in dates:  # 降序
            if counts.get(d, 0) >= total * 0.5:
                ref = d
                break
        if ref is None:
            ref = max(counts, key=lambda k: counts[k])

        pos = {d: i for i, d in enumerate(dates)}  # 0 = 最新
        ref_pos = pos.get(ref, 0)
        out: Dict[str, int] = {}
        for code, bars in bars_map.items():
            if not bars:
                out[code] = -1
                continue
            p = pos.get(bars[-1]["date"])
            if p is None:
                out[code] = window
            elif p - ref_pos > 1:      # 容忍 1 个交易日的刷新延迟
                out[code] = p - ref_pos
        return out

    async def trading_days(self, count: int) -> List[str]:
        """取最近 ``count`` 个交易日（YYYY-MM-DD），用上证指数日线作为日历。"""
        data = await self.svc.kline(_CALENDAR_INDEX, "daily", "none", max(count + 5, 20))
        days = [b["date"] for b in data.get("bars", []) if b.get("date")]
        return days[-count:]

    async def universe(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """全市场股票快照（SQLite 缓存 + 内存缓存，避免每次扫都打上游）。"""
        if self._universe_cache is not None and not refresh:
            return self._universe_cache
        # 股票列表变化很慢；缓存 30 天，避免上游限流时无法扫股
        cached = None if refresh else self.store.get_kv("universe:all", ttl=30 * 24 * 3600)
        if cached:
            self._universe_cache = cached
            self._names = {it["code"]: it.get("name") or "" for it in cached}
            return cached
        items = await self.svc.all_stocks("all")
        self._universe_cache = items
        self._names = {it["code"]: it.get("name") or "" for it in items}
        # 流通股本（换手/市值特征用），从 .data/meta.json 读
        import json as _json
        import os as _os

        meta_path = _os.path.join(self.settings.data_dir, "meta.json")
        if _os.path.exists(meta_path):
            try:
                self._meta_map = _json.load(open(meta_path, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self._meta_map = {}
        self._share_cache = None
        try:
            self.store.put_kv("universe:all", items)
        except Exception:  # noqa: BLE001
            pass
        return items

    async def limit_up_codes(
        self, lookback: int, kinds: Sequence[str] = ("zt",)
    ) -> Dict[str, str]:
        """最近 ``lookback`` 个交易日的涨停/炸板股并集，返回 ``{code: name}``。"""
        days = await self.trading_days(lookback)
        found: Dict[str, str] = {}

        async def fetch(day: str, kind: str) -> None:
            try:
                pool = await self.svc.em.limit_up_pool(day.replace("-", ""), kind)
            except Exception as exc:  # noqa: BLE001
                log.debug("涨停池 %s %s 获取失败: %s", day, kind, exc)
                return
            # 注意：push2ex 的 qdate 字段始终返回最新交易日，不能用来判断是否命中，
            # 只要 count>0 就说明该日数据有效（上游仅保留约 20 个自然日）。
            if not pool.get("count"):
                return
            for item in pool["items"]:
                found.setdefault(item["code"], item.get("name") or "")

        sem = asyncio.Semaphore(6)

        async def guarded(day: str, kind: str) -> None:
            async with sem:
                await fetch(day, kind)

        await asyncio.gather(*(guarded(d, k) for d in days for k in kinds))
        return found

    # ------------------------------------------------------------ 日线补拉

    async def ensure_bars(
        self,
        codes: Sequence[str],
        adjust: str = "none",
        min_bars: int = 70,
        stage: str = "拉取日线",
        tail: Optional[int] = None,
        force: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """确保这些代码的日线在本地缓存中（缺失的并发补拉）。

        ``tail`` 只回传每只股票最近 N 根（形态判定只需尾部数据，可大幅提速）。
        ``force=True`` 时忽略缓存直接重新拉取（用于把本地数据刷新到最新交易日）。
        """
        codes = list(dict.fromkeys(codes))
        if not codes:
            return {}

        cached = self.store.get_many(codes, adjust, tail=tail)
        floor = min(min_bars, tail or min_bars)
        if force:
            have: Dict[str, List[Dict[str, Any]]] = {}
            stale = list(codes)
        else:
            have = {c: b for c, b in cached.items() if len(b) >= floor}
            stale = [c for c in codes if len(cached.get(c, [])) < floor]
            stale = self.store.stale_codes(stale, adjust, min_bars)
        stale_set = set(stale)

        progress = ScanProgress(stage=stage, total=len(stale), cached=len(have))
        progress.callback = self.progress_cb
        if self.progress_cb:
            self.progress_cb(progress)
        if not stale:
            return have

        self.svc.set_concurrency(self.concurrency)
        sem = asyncio.Semaphore(self.concurrency)
        want = max(min_bars + 10, 80)

        async def work(code: str) -> None:
            async with sem:
                try:
                    data = await self.svc.kline(code, "daily", adjust, want)
                    bars = data.get("bars") or []
                except Exception:  # noqa: BLE001
                    bars = []
                if bars:
                    self.store.put_bars(code, adjust, bars)
                    if len(bars) >= min_bars:
                        have[code] = bars
                    progress.tick(True)
                else:
                    self.store.mark_failed(code, adjust)
                    progress.tick(False)

        await asyncio.gather(*(work(c) for c in stale_set))
        for c in codes:
            if c in stale_set:
                continue
            bars = cached.get(c)
            if bars and len(bars) >= floor:
                have[c] = bars
        return have

    # ------------------------------------------------------------ 策略一：美人肩

    async def scan_meirenjian(
        self,
        cfg: Optional[MeiRenJianConfig] = None,
        universe: str = "limitup",
        extra_codes: Optional[Iterable[str]] = None,
        top: int = 50,
    ) -> List[Dict[str, Any]]:
        """扫描美人肩形态。

        ``universe``:
          * ``limitup``（默认）— 只用最近 lookback 日的涨停股（快，几十~几百只）
          * ``all``           — 全市场扫描（慢，需要预热缓存）
        """
        cfg = cfg or MeiRenJianConfig()
        names: Dict[str, str] = {}

        if universe == "all":
            items = await self.universe()
            names = {it["code"]: it.get("name") or "" for it in items}
            names.update(await self.limit_up_codes(cfg.lookback))
        else:
            names = dict(await self.limit_up_codes(cfg.lookback))
            if extra_codes:
                await self.universe()
                for c in extra_codes:
                    names.setdefault(str(c), self._names.get(str(c), ""))

        codes = sorted(names)
        if not codes:
            return []

        bars_map = await self.ensure_bars(codes, "none", cfg.min_bars, "① 拉取候选股日线")

        # 补实时行情（现价 / 换手 / 成交额）
        progress = ScanProgress(stage="② 补实时行情", total=len(bars_map))
        progress.callback = self.progress_cb
        quotes: Dict[str, Dict[str, Any]] = {}
        quote_codes = list(bars_map)
        for i in range(0, len(quote_codes), 100):
            batch = quote_codes[i : i + 100]
            try:
                for row in await self.svc.quotes(batch):
                    quotes[row["code"]] = row
            except Exception as exc:  # noqa: BLE001
                log.debug("补行情失败: %s", exc)
            progress.tick(True, len(batch))

        results: List[Dict[str, Any]] = []
        for code, bars in bars_map.items():
            name = names.get(code) or quotes.get(code, {}).get("name") or ""
            hit = detect_meirenjian(bars, code, name, cfg, quotes.get(code))
            if hit:
                results.append(hit)

        results.sort(key=lambda r: (-r["score"], -r.get("整理天数", 0)))
        return results[:top] if top else results

    # ------------------------------------------------------------ 策略一：二次涨停（美人肩）

    async def scan_second_board(
        self,
        cfg: Optional[SecondBoardConfig] = None,
        top: int = 40,
        market: str = "main",
        min_score: float = 0.0,
        adjust: str = "qfq",
        extra_codes: Optional[Iterable[str]] = None,
        collect: bool = False,
        exclude_suspended: bool = True,
        refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """扫描「首板 → 整理 → 二次涨停」形态（即用户口径的美人肩）。

        以每只股票**最新一根 K 线**为决策点。提速关键：先用 SQL 把候选池从
        全市场 5900 只压到「最近 3~10 天内出现过涨幅≥9.5%」的几百只，
        再逐只做精确形态判定。

        ``collect=True`` 时不做结构过滤，返回所有首板候选（带 ``features``），
        供调用方用不同阈值反复筛选——自动放宽只需扫一次。
        """
        cfg = cfg or SecondBoardConfig()
        items = await self.universe()
        names = {it["code"]: it.get("name") or "" for it in items}
        snap = {it["code"]: it for it in items}

        # ---- SQL 层预筛（毫秒级）----
        cand = self.store.recent_limit_up_codes(
            adjust, lookback=cfg.max_gap + 3, threshold=9.5
        )
        codes = [c for c in cand if c in names]
        if market != "all":
            codes = [c for c in codes if self._match_market(c, market)]
        if extra_codes:
            codes = list(dict.fromkeys(codes + [str(c) for c in extra_codes]))

        bars_map = await self.ensure_bars(
            codes, adjust, cfg.min_bars, "① 拉取候选股日线", tail=cfg.indicator_bars,
            force=refresh,
        )

        from .context import get_context

        mctx = get_context(self.store)

        progress = ScanProgress(stage="② 形态匹配", total=len(bars_map))
        progress.callback = self.progress_cb
        if self.progress_cb:
            self.progress_cb(progress)

        results: List[Dict[str, Any]] = []
        for code, bars in bars_map.items():
            hit = detect_second_board(
                bars, code, names.get(code, ""), cfg, snap.get(code),
                collect=collect, float_shares=self._shares(code), mctx=mctx,
            )
            if collect:
                results.extend(hit)          # type: ignore[arg-type]
            elif hit and hit["score_raw"] >= min_score:
                results.append(hit)
            progress.tick(True)

        lag = self.suspended_lag(bars_map, adjust)
        for r in results:
            n = lag.get(r["code"], 0)
            if n > 0:
                r["停牌天数"] = n
        results = [r for r in results if not r.get("停牌天数")] if exclude_suspended else results
        results.sort(key=lambda r: -r["score_raw"])
        for i, r in enumerate(results, 1):
            r["rank"] = i
        return results[:top] if top else results

    @staticmethod
    def _match_market(code: str, market: str) -> bool:
        """按交易所/板块过滤。

        * ``main`` —— **沪深主板**（默认）：深市 000/001/002/003 + 沪市 600/601/603/605，
          **排除创业板 300/301 与科创板 688/689**（20% 涨跌幅，规则不同）
        * ``sz`` / ``sh`` —— 各自全部（含创业板 / 科创板）
        * ``zb`` —— 仅深市主板；``cyb`` 创业板；``kcb`` 科创板；``bj`` 北交所
        """
        if market == "main":
            return code.startswith(("000", "001", "002", "003",
                                    "600", "601", "603", "605"))
        if market == "sz":
            return code.startswith(("000", "001", "002", "003", "300", "301"))
        if market == "sh":
            return code.startswith(("600", "601", "603", "605", "688", "689"))
        if market == "cyb":
            return code.startswith(("300", "301"))
        if market == "kcb":
            return code.startswith(("688", "689"))
        if market == "zb":
            return code.startswith(("000", "001", "002", "003"))
        if market == "bj":
            return code.startswith(("4", "8", "920"))
        return True

    async def scan_second_board_ladder(
        self,
        cfg: Optional[SecondBoardConfig] = None,
        top: int = 40,
        min_score: float = 0.0,
        market: str = "main",
        relax: bool = True,
    ) -> Tuple[List[Dict[str, Any]], SecondBoardConfig, List[str]]:
        """命中不足时逐步降低门槛（结构条件基本不动）。"""
        from dataclasses import replace

        base = cfg or SecondBoardConfig()
        steps = [
            ("原始条件", base, min_score),
            ("横盘天数放宽到 2~20 天", replace(base, min_gap=2, max_gap=20), 0.0),
            ("收盘震幅放宽到 ±7%", replace(base, close_band=0.07, min_gap=2, max_gap=20), 0.0),
            ("总振幅放宽到 20%",
             replace(base, close_band=0.07, min_gap=2, max_gap=20, max_range_pct=0.20), 0.0),
        ]
        if not relax:
            steps = steps[:1]

        # ---- 只扫一次：用最宽松的结构条件收全候选，再在内存里逐级收紧 ----
        widest = replace(
            base,
            min_gap=min(s[1].min_gap for s in steps),
            max_gap=max(s[1].max_gap for s in steps),
            min_score=0.0,
        )
        cands = await self.scan_second_board(
            widest, top=0, min_score=0.0, market=market, collect=True
        )

        notes: List[str] = []
        for i, (label, c, ms) in enumerate(steps):
            sel = [
                h for h in cands
                if not structure_reasons(h["features"], c) and h["score_raw"] >= ms
            ]
            if i:
                notes.append(label)
            if sel:
                # 同一只股票可能有多个锚点，只保留打分最高的
                uniq: Dict[str, Dict[str, Any]] = {}
                for r in sel:
                    cur = uniq.get(r["code"])
                    if cur is None or r["score_raw"] > cur["score_raw"]:
                        uniq[r["code"]] = r
                sel = list(uniq.values())
                sel.sort(key=lambda r: -r["score_raw"])
                for j, r in enumerate(sel, 1):
                    r["rank"] = j
                for r in sel:
                    r.pop("features", None)
                return (sel[:top] if top else sel), c, notes
        return [], base, notes

    # ------------------------------------------------------------ 策略二：趋势票（涨不停不涨停）

    async def _refresh_quotes(self, results: List[Dict[str, Any]]) -> None:
        """用实时行情刷新展示字段。

        ``universe()`` 的全市场快照可能滞后（东方财富 clist 接口偶尔返回几天前的数据），
        但形态判定用的是本地日线缓存（最新交易日），所以只用实时行情覆盖展示字段。
        """
        if not results:
            return
        try:
            rows = await self.svc.quotes([r["code"] for r in results])
        except Exception as exc:  # noqa: BLE001
            log.debug("刷新行情失败: %s", exc)
            return
        qmap = {q.get("code"): q for q in rows if q.get("code")}
        for r in results:
            q = qmap.get(r["code"])
            if not q:
                continue
            if q.get("price"):
                r["现价"] = q["price"]
            for src, dst in (("change_pct", "今日涨跌%"), ("amount", "成交额"),
                             ("turnover_rate", "换手率%"), ("total_mv", "总市值"),
                             ("industry", "行业")):
                if q.get(src) is not None:
                    r[dst] = q[src]

    async def scan_steady_trend(
        self,
        cfg: Optional[SteadyTrendConfig] = None,
        top: int = 40,
        market: str = "main",
        adjust: str = "qfq",
        max_distance: Optional[float] = None,
        exclude_suspended: bool = True,
    ) -> List[Dict[str, Any]]:
        """扫描「像用户样本那样的趋势票」。

        与二次涨停不同，这里**没有自动放宽**：不符合就返回空列表，
        因为这种票本来就很少。只需要读全市场尾部日线 + 廉价预筛，
        只为通过的股票计算 MA/MACD。
        """
        cfg = cfg or SteadyTrendConfig()
        if max_distance is not None:
            cfg = replace(cfg, max_distance=max_distance)

        items = await self.universe()
        names = {it["code"]: it.get("name") or "" for it in items}
        snap = {it["code"]: it for it in items}

        codes = [c for c in names if c]
        if market != "all":
            codes = [c for c in codes if self._match_market(c, market)]

        # ---- SQL 预筛 + 日期范围读取（走索引，比窗口函数快很多）----
        keep = set(
            self.store.codes_with_recent_gain(adjust, days=cfg.window + 1, min_sum=0.0)
        )
        codes = [c for c in codes if c in keep]
        need = max(cfg.window + 150, 210)
        dates = self.store.recent_dates(adjust, need)
        since = dates[-1] if len(dates) >= need else None
        bars_map = self.store.get_many(
            codes, adjust, since=since, tail=None if since else need
        )

        progress = ScanProgress(stage="① 趋势形态匹配", total=len(bars_map))
        progress.callback = self.progress_cb
        if self.progress_cb:
            self.progress_cb(progress)

        results: List[Dict[str, Any]] = []
        for code, bars in bars_map.items():
            hit = detect_steady_trend(bars, code, names.get(code, ""), cfg, snap.get(code))
            if hit:
                results.append(hit)
            progress.tick(True)

        # 距离越小越像模板；停牌股排到最后
        await self._refresh_quotes(results)
        lag = self.suspended_lag(bars_map, adjust)
        for r in results:
            n = lag.get(r["code"], 0)
            if n > 0:
                r["停牌天数"] = n
        if exclude_suspended:
            results = [r for r in results if not r.get("停牌天数")]
        results.sort(key=lambda r: (r.get("停牌天数", 0) > 0, r["distance"]))
        for i, r in enumerate(results, 1):
            r["rank"] = i
        return results[:top] if top else results

    # ------------------------------------------------------------ 策略三：旧版涨不停不涨停

    async def scan_low_position(
        self,
        cfg: Optional[LowPosConfig] = None,
        top: int = 40,
        market: str = "main",
        adjust: str = "qfq",
        exclude_suspended: bool = True,
    ) -> List[Dict[str, Any]]:
        """扫描「低位均线粘合 + 拐头向上」的票（用户 2026-09 的两个样本）。

        与趋势票不同，这里没有自动放宽：不符合就返回空。
        全市场读取 300 根尾部日线 → 廉价预筛（暴跌+低位）→ 只为幸存者算 MA/MACD。
        """
        cfg = cfg or LowPosConfig()
        items = await self.universe()
        names = {it["code"]: it.get("name") or "" for it in items}
        snap = {it["code"]: it for it in items}

        codes = [c for c in names if c]
        if market != "all":
            codes = [c for c in codes if self._match_market(c, market)]

        # ---- 读取最近 300 根日线（走日期索引，一次拿完）----
        need = max(cfg.min_bars + 40, 300)
        dates = self.store.recent_dates(adjust, need)
        since = dates[-1] if len(dates) >= need else None
        bars_map = self.store.get_many(
            codes, adjust, since=since, tail=None if since else need
        )

        progress = ScanProgress(stage="① 低位粘合形态", total=len(bars_map))
        progress.callback = self.progress_cb
        if self.progress_cb:
            self.progress_cb(progress)

        results: List[Dict[str, Any]] = []
        for code, bars in bars_map.items():
            hit = detect_low_position(bars, code, names.get(code, ""), cfg, snap.get(code))
            if hit:
                results.append(hit)
            progress.tick(True)

        # 得分越高越好；停牌股排到最后
        await self._refresh_quotes(results)
        lag = self.suspended_lag(bars_map, adjust)
        for r in results:
            n = lag.get(r["code"], 0)
            if n > 0:
                r["停牌天数"] = n
        if exclude_suspended:
            results = [r for r in results if not r.get("停牌天数")]
        results.sort(key=lambda r: (r.get("停牌天数", 0) > 0, -r.get("score", 0.0)))
        for i, r in enumerate(results, 1):
            r["rank"] = i
        return results[:top] if top else results

    # ------------------------------------------------------------ 策略四：旧版涨不停不涨停

    async def scan_steady_climb(
        self,
        cfg: Optional[SteadyClimbConfig] = None,
        market: str = "all",
        top: int = 50,
        max_scan: int = 0,
    ) -> List[Dict[str, Any]]:
        """扫描「涨不停不涨停」形态（全市场）。"""
        cfg = cfg or SteadyClimbConfig()
        items = await self.universe()
        names = {it["code"]: it.get("name") or "" for it in items}

        # ---- 用实时快照预筛，把候选压到 1/3 以内 ----
        candidates: List[Dict[str, Any]] = []
        for it in items:
            if cfg.exclude_st and is_st(it.get("name") or ""):
                continue
            price = it.get("price")
            pct = it.get("change_pct")
            if not price or price < cfg.min_price:
                continue
            if pct is None:
                continue
            limit_pct = _limit_pct(it["code"], it.get("name") or "")
            # 今天可以是小跌（allow_down>0 时），但跌幅不能超过策略允许的回撤上限；
            # 也不能涨停。这里只做「宽筛」，保证不误伤命中项。
            if pct > min(cfg.max_daily_gain, limit_pct - cfg.limit_margin):
                continue
            if pct < -cfg.max_drawdown:
                continue
            if cfg.min_amount and (it.get("amount") or 0) < cfg.min_amount:
                continue
            if cfg.min_market_cap and (it.get("total_mv") or 0) < cfg.min_market_cap:
                continue
            candidates.append(it)

        candidates.sort(key=lambda r: -(r.get("amount") or 0))
        if max_scan:
            candidates = candidates[:max_scan]
        codes = [c["code"] for c in candidates]
        if not codes:
            return []

        bars_map = await self.ensure_bars(
            codes, "none", max(cfg.min_bars, cfg.days + 25), "① 拉取候选股日线"
        )

        results: List[Dict[str, Any]] = []
        for code, bars in bars_map.items():
            hit = detect_steady_climb(bars, code, names.get(code, ""), cfg, None)
            if hit:
                results.append(hit)

        # 用本地快照补行情字段
        snap = {c["code"]: c for c in candidates}
        for r in results:
            s = snap.get(r["code"], {})
            for src, dst in (("amount", "成交额"), ("turnover_rate", "换手率%"),
                             ("total_mv", "总市值"), ("change_pct", "今日涨幅%")):
                if s.get(src) is not None:
                    r.setdefault(dst, s[src])

        results.sort(key=lambda r: (-r["score"], -r["累计涨幅%"]))
        return results[:top] if top else results

    # ------------------------------------------------------------ 自动放宽

    async def scan_meirenjian_ladder(
        self,
        cfg: Optional[MeiRenJianConfig] = None,
        universe: str = "limitup",
        top: int = 30,
        relax: bool = True,
    ) -> Tuple[List[Dict[str, Any]], MeiRenJianConfig, List[str]]:
        """命中为空时逐步放宽条件，保证总有结果且告知放宽过程。"""
        from dataclasses import replace

        base = cfg or MeiRenJianConfig()
        steps: List[Tuple[str, MeiRenJianConfig]] = [("原始条件", base)]
        if relax:
            steps += [
                (f"浮动幅度放宽到 ±{base.band * 1.25 * 100:.1f}%",
                 replace(base, band=round(base.band * 1.25, 4))),
                (f"浮动 ±{base.band * 1.5 * 100:.1f}% + 允许 {base.allow_breach + 1} 天越界",
                 replace(base, band=round(base.band * 1.5, 4),
                         allow_breach=base.allow_breach + 1)),
                (f"浮动 ±{base.band * 1.75 * 100:.1f}% + 允许 {base.allow_breach + 2} 天越界 + 整理 ≥2 天",
                 replace(base, band=round(base.band * 1.75, 4),
                         allow_breach=base.allow_breach + 2, min_days=2)),
                (f"浮动 ±{base.band * 2 * 100:.1f}% + 放宽缩量要求",
                 replace(base, band=round(base.band * 2, 4), allow_breach=base.allow_breach + 2,
                         min_days=2, max_volume_ratio=round(base.max_volume_ratio * 1.5, 2))),
            ]
        notes: List[str] = []
        for i, (label, c) in enumerate(steps):
            results = await self.scan_meirenjian(c, universe=universe, top=top)
            if i:
                notes.append(label)
            if results:
                return results, c, notes
        return [], base, notes

    async def scan_steady_climb_ladder(
        self,
        cfg: Optional[SteadyClimbConfig] = None,
        top: int = 30,
        max_scan: int = 0,
        relax: bool = True,
    ) -> Tuple[List[Dict[str, Any]], SteadyClimbConfig, List[str]]:
        from dataclasses import replace

        base = cfg or SteadyClimbConfig()
        steps: List[Tuple[str, SteadyClimbConfig]] = [("原始条件", base)]
        if relax:
            steps += [
                (f"允许 {base.allow_down + 1} 天收跌", replace(base, allow_down=base.allow_down + 1)),
                (f"允许 {base.allow_down + 1} 天收跌 + 最大单日跌 {base.max_drawdown + 1:.1f}%",
                 replace(base, allow_down=base.allow_down + 1, max_drawdown=base.max_drawdown + 1)),
                (f"考察期缩短到 {max(4, base.days - 2)} 天",
                 replace(base, days=max(4, base.days - 2), allow_down=base.allow_down + 1,
                         max_drawdown=base.max_drawdown + 1)),
                ("不要求均线多头排列",
                 replace(base, days=max(4, base.days - 2), allow_down=base.allow_down + 2,
                         max_drawdown=base.max_drawdown + 1.5, require_ma_bull=False)),
            ]
        notes: List[str] = []
        for i, (label, c) in enumerate(steps):
            results = await self.scan_steady_climb(c, top=top, max_scan=max_scan)
            if i:
                notes.append(label)
            if results:
                return results, c, notes
        return [], base, notes

    # ------------------------------------------------------------ 预热

    async def warmup(self, limit: int = 0) -> Dict[str, Any]:
        """把全市场日线拉进本地缓存（只需跑一次，之后扫描秒出）。"""
        items = await self.universe(refresh=True)
        codes = [it["code"] for it in items]
        if limit:
            codes = codes[:limit]
        began = time.monotonic()
        bars_map = await self.ensure_bars(codes, "none", 70, "预热全市场日线")
        return {
            "requested": len(codes),
            "success": len(bars_map),
            "elapsed": round(time.monotonic() - began, 1),
            "store": self.store.stats(),
        }


def _limit_pct(code: str, name: str) -> float:
    from .indicators import board_limit_ratio

    return board_limit_ratio(code, name) * 100.0
