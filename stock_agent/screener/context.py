"""大盘 / 板块上下文特征。

思路
----
美人肩能不能二次涨停，很大程度上取决于**环境**：

* **大盘**（上证指数）：大盘走强时，个股更容易封板
* **板块**：板块整体在涨，龙头/跟风股更容易连板

实现
----
* 大盘：读 ``.data/market_ctx.json`` 里的上证指数日线
* 板块：**用成分股等权合成板块指数**（与个股口径一致，不需要额外接口）
* 市场宽度：用本地全市场日线算「上涨家数占比」「涨停家数」

所有特征按日期预计算成字典，挖掘时 O(1) 查表。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from .indicators import macd, mean, moving_average

log = logging.getLogger(__name__)

# 输出给 extract_setup 的特征名
CONTEXT_FEATURE_KEYS = (
    "mkt_ret_5d",       # 上证 5 日涨幅 %
    "mkt_ret_20d",      # 上证 20 日涨幅 %
    "mkt_dev_ma20",     # 上证偏离 20 日线 %
    "mkt_hist_n",       # 上证 MACD 柱 / 指数
    "mkt_week_hist",    # 上证周线 MACD 柱 / 指数
    "mkt_vol_ratio",    # 上证成交量 / 20 日均量
    "mkt_breadth",      # 全市场上涨家数占比 0~1
    "mkt_limitup",      # 全市场涨停家数 / 100
    "sec_ret_5d",       # 板块 5 日涨幅 %
    "sec_ret_20d",      # 板块 20 日涨幅 %
    "sec_dev_ma20",     # 板块偏离 20 日线 %
    "sec_hist_n",       # 板块 MACD 柱 / 指数
    "sec_rank_5d",      # 板块 5 日涨幅在全板块中的分位 0~1
    "ctx_ok",           # 1 = 该日大盘/板块数据齐全（缺失时不做环境过滤）
)


def _weekly_of(dates: List[str], closes: List[float]) -> List[float]:
    """把日线压成周线收盘（ISO 周）。"""
    from datetime import datetime

    out: List[float] = []
    key = None
    for d, c in zip(dates, closes):
        try:
            k = (d[:4], datetime.strptime(d, "%Y-%m-%d").isocalendar()[1])
        except Exception:  # noqa: BLE001
            k = d[:7]
        if k != key:
            key = k
            out.append(c)
        else:
            out[-1] = c
    return out


def _series_features(
    dates: List[str], closes: List[float], vols: Optional[List[float]] = None
) -> Dict[str, Dict[str, float]]:
    """对一条指数序列算逐日特征。"""
    n = len(closes)
    ma20 = moving_average(closes, 20)
    dif, dea, hist = macd(closes)
    wclose = _weekly_of(dates, closes)
    # 周线 MACD 需要按周对齐，这里用「截至该周的周线序列」逐日算（简化：整段算完再映射）
    _, _, whist = macd(wclose)
    # 建立 日 -> 周序号 映射
    from datetime import datetime

    wk_idx: List[int] = []
    key = None
    k = -1
    for d in dates:
        try:
            kk = (d[:4], datetime.strptime(d, "%Y-%m-%d").isocalendar()[1])
        except Exception:  # noqa: BLE001
            kk = d[:7]
        if kk != key:
            key = kk
            k += 1
        wk_idx.append(k)

    out: Dict[str, Dict[str, float]] = {}
    for i in range(n):
        c = closes[i]
        if not c:
            continue
        r5 = (c / closes[i - 5] - 1) * 100 if i >= 5 and closes[i - 5] else 0.0
        r20 = (c / closes[i - 20] - 1) * 100 if i >= 20 and closes[i - 20] else 0.0
        dev = ((c - ma20[i]) / ma20[i] * 100) if ma20[i] else 0.0
        h = hist[i] if i < len(hist) and hist[i] is not None else 0.0
        w = whist[wk_idx[i]] if wk_idx[i] < len(whist) and whist[wk_idx[i]] is not None else 0.0
        vr = 1.0
        if vols and i >= 20:
            base = mean(vols[i - 20 : i])
            if base:
                vr = vols[i] / base
        out[dates[i]] = {
            "ret_5d": r5, "ret_20d": r20, "dev_ma20": dev,
            "hist_n": (h / c * 100) if c else 0.0,
            "week_hist": (w / c * 100) if c else 0.0,
            "vol_ratio": vr,
        }
    return out


class MarketContext:
    """大盘 + 板块上下文查表器。"""

    def __init__(self, store: Any, ctx_path: Optional[str] = None) -> None:
        self.store = store
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.path = ctx_path or os.path.join(root, ".data", "market_ctx.json")
        self.market: Dict[str, Dict[str, float]] = {}
        self.sector: Dict[str, Dict[str, Dict[str, float]]] = {}
        self.code_sector: Dict[str, str] = {}
        self.breadth: Dict[str, Dict[str, float]] = {}
        self._load()

    # ------------------------------------------------------------ 构建

    def _load(self) -> None:
        if not os.path.exists(self.path):
            log.warning("上下文文件不存在: %s", self.path)
            return
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.code_sector = data.get("code_sector") or {}

        # ---- 大盘 ----
        rows = data.get("index") or []
        dates = [r[0] for r in rows]
        closes = [float(r[2]) for r in rows]     # [date, open, close, high, low, vol]
        vols = [float(r[5]) if len(r) > 5 and r[5] else 0.0 for r in rows]
        mk = _series_features(dates, closes, vols)
        for d, f in mk.items():
            self.market[d] = {
                "mkt_ret_5d": f["ret_5d"], "mkt_ret_20d": f["ret_20d"],
                "mkt_dev_ma20": f["dev_ma20"], "mkt_hist_n": f["hist_n"],
                "mkt_week_hist": f["week_hist"], "mkt_vol_ratio": f["vol_ratio"],
            }
        log.info("大盘特征 %d 天（%s ~ %s）", len(self.market),
                 dates[0] if dates else "-", dates[-1] if dates else "-")

        self._build_sector()

    def _build_sector(self) -> None:
        """用本地成分股等权合成板块指数，并算特征。"""
        if not self.code_sector:
            return
        try:
            with self.store._connect() as conn:  # noqa: SLF001
                conn.row_factory = None
                codes = [
                    r[0] for r in conn.execute(
                        "SELECT DISTINCT code FROM daily_bars WHERE adjust='bfq'"
                    )
                ]
            # 板块指数只需要近端收益，取最近 ~120 个交易日即可
            # （用 since 走日期索引，比 tail 的窗口函数快几十倍）
            _d = self.store.recent_dates("bfq", 120)
            bars_map = self.store.get_many(codes, "bfq", since=_d[-1]) if _d else {}
        except Exception as exc:  # noqa: BLE001
            log.warning("板块合成失败: %s", exc)
            return
        if not bars_map:
            return

        # date -> sector -> [累计收益]
        daily: Dict[str, Dict[str, List[float]]] = {}
        all_dates: set = set()
        for code, bars in bars_map.items():
            sec = self.code_sector.get(code)
            if not sec:
                continue
            prev = None
            for b in bars:
                c, d = b.get("close"), b.get("date")
                if c is None or not d:
                    continue
                all_dates.add(d)
                if prev:
                    daily.setdefault(d, {}).setdefault(sec, []).append((c / prev - 1) * 100)
                prev = c

        # 板块日收益 -> 指数序列
        for sec in {v for v in self.code_sector.values()}:
            sd = sorted(d for d in all_dates if sec in daily.get(d, {}))
            if len(sd) < 40:
                continue
            level = 100.0
            closes: List[float] = []
            dates: List[str] = []
            for d in sd:
                rs = daily[d].get(sec) or []
                if not rs:
                    continue
                level *= (1 + mean(rs) / 100.0)
                dates.append(d)
                closes.append(level)
            feats = _series_features(dates, closes)
            self.sector[sec] = feats

        # 板块 5 日涨幅排名分位
        by_date: Dict[str, List[tuple]] = {}
        for sec, feats in self.sector.items():
            for d, f in feats.items():
                by_date.setdefault(d, []).append((f["ret_5d"], sec))
        for d, lst in by_date.items():
            lst.sort()
            n = len(lst)
            for i, (_, sec) in enumerate(lst):
                self.sector[sec][d]["rank_5d"] = i / max(n - 1, 1)

        # ---- 市场宽度 ----
        for d, secs in daily.items():
            ups = tot = 0
            for rs in secs.values():
                for r in rs:
                    tot += 1
                    if r > 0:
                        ups += 1
            if tot:
                self.breadth[d] = {"mkt_breadth": ups / tot}
        log.info("板块特征 %d 个板块；宽度 %d 天", len(self.sector), len(self.breadth))

    # ------------------------------------------------------------ 查询

    def features(self, code: str, date: str) -> Dict[str, float]:
        """返回该 (股票, 日期) 的上下文特征。"""
        out = {k: 0.0 for k in CONTEXT_FEATURE_KEYS}
        mk = self.market.get(date)
        out["ctx_ok"] = 1.0 if mk else 0.0
        if mk:
            out.update(mk)
        bd = self.breadth.get(date)
        if bd:
            out["mkt_breadth"] = bd["mkt_breadth"]
        sec = self.code_sector.get(code)
        if sec:
            sf = (self.sector.get(sec) or {}).get(date)
            if sf:
                out["sec_ret_5d"] = sf["ret_5d"]
                out["sec_ret_20d"] = sf["ret_20d"]
                out["sec_dev_ma20"] = sf["dev_ma20"]
                out["sec_hist_n"] = sf["hist_n"]
                out["sec_rank_5d"] = sf.get("rank_5d", 0.5)
        return out


_SHARED: Optional[MarketContext] = None


def get_context(store: Any = None) -> Optional[MarketContext]:
    """进程内单例。"""
    global _SHARED
    if _SHARED is None:
        if store is None:
            from .scanner import default_store_path
            from .store import ScreenerStore

            store = ScreenerStore(default_store_path())
        try:
            _SHARED = MarketContext(store)
        except Exception as exc:  # noqa: BLE001
            log.warning("上下文加载失败: %s", exc)
            return None
    return _SHARED
