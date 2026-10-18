"""研究步骤 28：低位均线粘合（低位票）形态 —— 特征、样本与阈值标定。

形态（用户 2026-09 提供）：

    前期暴跌（250 日高位回撤 ≥ 25%）→ 在低位（250 日区间下半部）
    多根均线（MA5/10/15/20）粘合（离散度 ≤ 3%）
    → 均线拐头向上（MA5 上穿 MA10、收盘站上均线、MACD 转好）

样本：

* ``605366 宏柏新材`` @2026-09-14  四线离散度 1.06%，其后 5 日最高 +7.1%
* ``002990 盛视科技`` @2026-09-14  四线离散度 2.05%，其后 5 日最高 +15.7%

本脚本：

1. 对全市场（默认沪深主板）逐日提取「低位粘合」特征（numpy 向量化）；
2. 统计各阈值组合下的信号数 / 未来 5/10/20 日收益 / 摸 +10%、+15% 概率；
3. 与「全市场基准」「低位基准（只满足暴跌+低位）」对比；
4. 输出分年度与去重（同股 20 日冷却）口径。

用法::

    python research/05_lowpos/28_lowpos_study.py --start 2020-01-01
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.scanner import default_store_path  # noqa: E402
from stock_agent.screener.store import ScreenerStore  # noqa: E402

ROOT = _p
NAMES = os.path.join(ROOT, ".data", "names.json")

Row = Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]


def keep_market(code: str, market: str) -> bool:
    if code.startswith(("4", "8", "920")):
        return False
    if market == "main":
        return code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))
    if market == "sz":
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    if market == "sh":
        return code.startswith(("600", "601", "603", "605", "688", "689"))
    return True


def ma_np(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out[w - 1:] = (cs[w:] - cs[:-w]) / w
    return out


def ema_np(x: np.ndarray, span: int) -> np.ndarray:
    k = 2.0 / (span + 1.0)
    out = np.empty_like(x)
    prev = x[0]
    out[0] = prev
    for i in range(1, len(x)):
        prev = x[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def macd_np(c: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    dif = ema_np(c, 12) - ema_np(c, 26)
    dea = ema_np(dif, 9)
    hist = (dif - dea) * 2.0
    return dif, dea, hist


def roll_min(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if len(x) < w:
        return out
    out[w - 1:] = np.lib.stride_tricks.sliding_window_view(x, w).min(axis=1)
    return out


def roll_any(x: np.ndarray, w: int) -> np.ndarray:
    """最近 w 天内是否有 True（含当天），前 w-1 个用部分窗口。"""
    out = np.zeros(x.shape, dtype=bool)
    n = len(x)
    for i in range(n):
        out[i] = bool(x[max(0, i - w + 1): i + 1].any())
    return out


def features(bars: List[Dict[str, Any]]) -> Optional[Dict[str, np.ndarray]]:
    """对单只股票计算逐日特征数组（下标 0 = 最早）。"""
    try:
        c = np.array([float(b["close"]) for b in bars])
        h = np.array([float(b["high"]) for b in bars])
        l = np.array([float(b["low"]) for b in bars])
        v = np.array([float(b.get("volume") or 0.0) for b in bars])
    except (TypeError, ValueError):
        return None
    n = len(c)
    if n < 260:
        return None

    ma5, ma10, ma15 = ma_np(c, 5), ma_np(c, 10), ma_np(c, 15)
    ma20, ma30, ma60 = ma_np(c, 20), ma_np(c, 30), ma_np(c, 60)
    m4 = np.vstack([ma5, ma10, ma15, ma20])
    spread4 = (m4.max(axis=0) - m4.min(axis=0)) / (m4.mean(axis=0) + 1e-12)
    spread_min10 = roll_min(spread4, 10)
    spread_min5 = roll_min(spread4, 5)

    hh250 = np.full(n, np.nan)
    ll250 = np.full(n, np.nan)
    if n >= 250:
        hh250[249:] = np.lib.stride_tricks.sliding_window_view(h, 250).max(axis=1)
        ll250[249:] = np.lib.stride_tricks.sliding_window_view(l, 250).min(axis=1)
    hh120 = np.full(n, np.nan)
    ll120 = np.full(n, np.nan)
    if n >= 120:
        hh120[119:] = np.lib.stride_tricks.sliding_window_view(h, 120).max(axis=1)
        ll120[119:] = np.lib.stride_tricks.sliding_window_view(l, 120).min(axis=1)
    hh60 = np.full(n, np.nan)
    ll60 = np.full(n, np.nan)
    low_age60 = np.full(n, np.nan)
    if n >= 60:
        w60h = np.lib.stride_tricks.sliding_window_view(h, 60)
        w60l = np.lib.stride_tricks.sliding_window_view(l, 60)
        hh60[59:] = w60h.max(axis=1)
        ll60[59:] = w60l.min(axis=1)
        low_age60[59:] = 59 - w60l.argmin(axis=1)
    low_age40 = np.full(n, np.nan)
    if n >= 40:
        w40l = np.lib.stride_tricks.sliding_window_view(l, 40)
        low_age40[39:] = 39 - w40l.argmin(axis=1)
    low_age120 = np.full(n, np.nan)
    if n >= 120:
        w120l = np.lib.stride_tricks.sliding_window_view(l, 120)
        low_age120[119:] = 119 - w120l.argmin(axis=1)
    dd250 = (hh250 - ll250) / (hh250 + 1e-12)
    dd120 = (hh120 - ll120) / (hh120 + 1e-12)
    dd60 = (hh60 - ll60) / (hh60 + 1e-12)
    pos250 = (c - ll250) / (hh250 - ll250 + 1e-12)
    vs_hh250 = c / (hh250 + 1e-12)
    conv_ratio3 = ma_np((spread4 <= 0.030).astype(float), 10)
    conv_ratio2 = ma_np((spread4 <= 0.020).astype(float), 10)

    dif, dea, hist = macd_np(c)
    hist_prev = np.roll(hist, 1)
    hist_prev[0] = hist[0]
    hist_up = (hist > hist_prev).astype(float)

    def slope_arr(x: np.ndarray, w: int) -> np.ndarray:
        out = np.full(n, np.nan)
        out[w:] = (x[w:] - x[:-w]) / (np.abs(x[:-w]) + 1e-12)
        return out

    vol5, vol20 = ma_np(v, 5), ma_np(v, 20)
    ret60 = np.full(n, np.nan)
    ret120 = np.full(n, np.nan)
    ret60[60:] = c[60:] / (c[:-60] + 1e-12) - 1.0
    ret120[120:] = c[120:] / (c[:-120] + 1e-12) - 1.0

    return {
        "c": c, "h": h, "l": l,
        "ma5": ma5, "ma10": ma10, "ma15": ma15, "ma20": ma20,
        "ma30": ma30, "ma60": ma60,
        "spread4": spread4, "spread_min10": spread_min10, "spread_min5": spread_min5,
        "hh250": hh250, "ll250": ll250, "hh120": hh120, "ll120": ll120,
        "hh60": hh60, "ll60": ll60, "low_age60": low_age60, "low_age40": low_age40,
        "low_age120": low_age120,
        "conv_ratio3": conv_ratio3, "conv_ratio2": conv_ratio2,
        "dd250": dd250, "dd120": dd120, "dd60": dd60,
        "pos250": pos250, "vs_hh250": vs_hh250,
        "ret20": np.concatenate([np.full(20, np.nan), c[20:] / (c[:-20] + 1e-12) - 1.0]),
        "dif": dif, "dea": dea, "hist": hist, "hist_up": hist_up,
        "ma5_slope": slope_arr(ma5, 3), "ma10_slope": slope_arr(ma10, 3),
        "ma20_slope": slope_arr(ma20, 3), "ma30_slope": slope_arr(ma30, 3),
        "vol_ratio": vol5 / (vol20 + 1e-12),
        "bias5": c / (ma5 + 1e-12) - 1.0, "bias20": c / (ma20 + 1e-12) - 1.0,
        "ret60": ret60, "ret120": ret120,
    }


# ---------------- 变体定义 ----------------
# 每个变体都是 dict，缺省值见 DEFAULTS。
DEFAULTS: Dict[str, Any] = {
    "dd250": 0.25,      # 250 日区间回撤下限（前期暴跌幅度）
    "pos250": 0.55,     # 250 日区间位置上限（低位）
    "vs_hh250": 0.75,   # 收盘 / 250 日最高价 上限
    "dd120": 0.0,       # 近 120 日回撤下限
    "dd60": 0.0,        # 近 60 日回撤下限（最新一段暴跌）
    "low_age": 999,     # 距近 60 日最低点的天数上限（暴跌低点要新鲜）
    "low_age120": 999,  # 距近 120 日最低点的天数上限
    "smin10": 0.030,    # 近 10 日四线最小离散度上限（粘合）
    "conv_ratio": 0.0,  # 近 10 日中 spread4<=3% 的天数占比下限（真粘合）
    "conv_ratio2": 0.0, # 近 10 日中 spread4<=2% 的天数占比下限
    "cross": 5,         # N 日内出现 MA5 上穿 MA10（0=不要求）
    "s_cross": 0.040,   # 金叉当天的离散度上限
    "s_now": 0.060,     # 当前离散度上限（已经拉开的不要）
    "hist": "ge0",      # ge0=红柱; up=红柱放大; any=不要求
    "vol_min": 0.0,     # 量比(5日/20日)下限
    "vol_ratio": 0.0,   # 当前量 / 前20日均量 下限（放量）
    "bias20_lo": -0.99, "bias20_hi": 0.99,  # 收盘相对 MA20 的偏离区间
    "ret20_max": 0.99,  # 近 20 日涨幅上限（还没涨起来）
    "ma20_slope": -0.99,  # MA20 3日斜率下限
}

BASE_REF: Dict[str, Any] = {"dd250": 0.25, "pos250": 0.55, "vs_hh250": 0.75,
                            "smin10": 0.030, "cross": 0, "hist": "ge0"}

VARIANTS: List[Dict[str, Any]] = [
    {"name": "LOOSE", "cross": 0, "hist": "ge0", "pos250": 0.45, "vs_hh250": 0.70},
    {"name": "X5_s030", "cross": 5, "s_now": 0.05},
    {"name": "DEFAULT", "dd250": 0.30, "pos250": 0.45, "dd120": 0.35, "low_age": 45,
     "smin10": 0.025, "cross": 5, "s_now": 0.04,
     "bias20_lo": -0.06, "bias20_hi": 0.08},
    {"name": "VOL", "dd250": 0.30, "pos250": 0.45, "dd120": 0.35, "low_age": 45,
     "smin10": 0.025, "cross": 5, "s_now": 0.04,
     "bias20_lo": -0.06, "bias20_hi": 0.08, "vol_min": 1.1},
    {"name": "STRICT", "dd250": 0.30, "pos250": 0.45, "dd120": 0.35, "low_age120": 60,
     "smin10": 0.025, "conv_ratio": 0.6, "cross": 5, "s_now": 0.04,
     "bias20_lo": -0.06, "bias20_hi": 0.08},
    {"name": "CROSS10", "dd250": 0.30, "pos250": 0.45, "dd120": 0.35, "low_age": 45,
     "smin10": 0.025, "cross": 10, "s_now": 0.05,
     "bias20_lo": -0.06, "bias20_hi": 0.08},
]

TURN_DESC = {
    "ge0": "MACD柱≥0", "up": "MACD柱放大", "any": "不看MACD",
    "cross": "MACD柱刚翻红（3日内）",
}


def signal_mask(f: Dict[str, np.ndarray], vv: Dict[str, Any], idx: np.ndarray
                ) -> Tuple[np.ndarray, np.ndarray]:
    v = dict(DEFAULTS)
    v.update(vv)
    g = lambda k: f[k][idx]
    base = ((g("dd250") >= v["dd250"]) & (g("pos250") <= v["pos250"])
            & (g("vs_hh250") <= v["vs_hh250"]))
    if v["dd120"]:
        base &= g("dd120") >= v["dd120"]
    if v["dd60"]:
        base &= g("dd60") >= v["dd60"]
    if v["low_age"] < 999:
        base &= g("low_age60") <= v["low_age"]
    if v["low_age120"] < 999:
        base &= g("low_age120") <= v["low_age120"]
    conv = g("spread_min10") <= v["smin10"]
    if v["conv_ratio"]:
        conv &= g("conv_ratio3") >= v["conv_ratio"]
    if v["conv_ratio2"]:
        conv &= g("conv_ratio2") >= v["conv_ratio2"]

    cl, ma5, ma10, ma20 = g("c"), g("ma5"), g("ma10"), g("ma20")
    hist, s20 = g("hist"), g("ma20_slope")
    up = np.ones(len(idx), dtype=bool)
    if v["cross"]:
        up &= roll_any((ma5 > ma10) & (np.roll(ma5, 1) <= np.roll(ma10, 1))
                       & (g("spread4") <= v["s_cross"]), v["cross"])
        up &= g("spread4") <= v["s_now"]
    else:
        up &= ma5 > ma10
    up &= cl >= ma5 * 0.99
    if v["hist"] == "ge0":
        up &= hist >= 0
    elif v["hist"] == "up":
        up &= (hist >= 0) & (g("hist_up") > 0.5)
    elif v["hist"] == "cross":
        up &= roll_any((hist > 0) & (np.roll(hist, 1) <= 0), 3)
    if v["vol_min"]:
        up &= g("vol_ratio") >= v["vol_min"]
    if v["vol_ratio"]:
        up &= g("vol_ratio") >= v["vol_ratio"]
    bias20 = g("bias20")
    up &= (bias20 >= v["bias20_lo"]) & (bias20 <= v["bias20_hi"])
    if v["ret20_max"] < 0.99:
        up &= g("ret20") <= v["ret20_max"]
    if v["ma20_slope"] > -0.99:
        up &= s20 >= v["ma20_slope"]
    return base & conv & up, base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adjust", default="qfq")
    ap.add_argument("--market", default="main", choices=["all", "main", "sz", "sh"])
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--min-bars", type=int, default=260)
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    store = ScreenerStore(default_store_path())
    with store._connect() as conn:
        conn.row_factory = None
        codes = [
            r[0] for r in conn.execute(
                "SELECT code FROM fetch_state WHERE adjust=? AND bars>=? ORDER BY code",
                (args.adjust, args.min_bars),
            )
        ]
    codes = [c for c in codes if keep_market(c, args.market)]
    if args.limit:
        codes = codes[: args.limit]
    print(f"标的池: {len(codes)} 只  （{args.market}，评估 {args.start} 起）")

    acc: Dict[str, List[Row]] = {v["name"]: [] for v in VARIANTS}
    acc["__base_low__"] = []
    acc["__base_all__"] = []
    trade_days: set = set()

    t0 = time.time()
    processed = 0
    for start in range(0, len(codes), args.batch):
        batch = codes[start: start + args.batch]
        bars_map = store.get_many(batch, args.adjust)
        for code in batch:
            bars = bars_map.get(code) or []
            if len(bars) < args.min_bars:
                continue
            f = features(bars)
            if f is None:
                continue
            dates = np.array([str(b.get("date")) for b in bars])
            c, h, l = f["c"], f["h"], f["l"]
            n = len(c)
            i0 = max(int(np.searchsorted(dates, args.start)), 249)
            i1 = n - 20
            if i1 <= i0:
                continue
            rr = np.arange(i0, i1)
            ok = (~np.isnan(f["dd250"][rr])) & (~np.isnan(f["spread_min10"][rr]))
            rr = rr[ok]
            if not len(rr):
                continue

            f5 = np.full(len(rr), np.nan)
            f10 = np.full(len(rr), np.nan)
            f20 = np.full(len(rr), np.nan)
            mx = np.full(len(rr), np.nan)
            mn = np.full(len(rr), np.nan)
            for k, i in enumerate(rr):
                f5[k] = c[i + 5] / c[i] - 1 if i + 5 < n else np.nan
                f10[k] = c[i + 10] / c[i] - 1 if i + 10 < n else np.nan
                if i + 20 < n:
                    f20[k] = c[i + 20] / c[i] - 1
                    mx[k] = h[i + 1: i + 21].max() / c[i] - 1
                    mn[k] = l[i + 1: i + 21].min() / c[i] - 1
            keep = ~np.isnan(f20)
            rr_k = rr[keep]
            f5, f10, f20, mx, mn = f5[keep], f10[keep], f20[keep], mx[keep], mn[keep]
            codes_k = np.array([code] * len(rr_k))
            dates_k = dates[rr_k]
            trade_days.update(dates_k.tolist())

            acc["__base_all__"].append((codes_k, dates_k, f5, f10, f20, mx, mn))
            base_cfg = dict(DEFAULTS)
            base_cfg.update(next(v for v in VARIANTS if v["name"] == "DEFAULT"))
            _, base = signal_mask(f, base_cfg, rr_k)
            if base.any():
                acc["__base_low__"].append((codes_k[base], dates_k[base], f5[base],
                                            f10[base], f20[base], mx[base], mn[base]))
            for v in VARIANTS:
                sig, _ = signal_mask(f, v, rr_k)
                if sig.any():
                    acc[v["name"]].append((codes_k[sig], dates_k[sig], f5[sig], f10[sig],
                                           f20[sig], mx[sig], mn[sig]))
        processed += len(batch)
        print(f"\r  已处理 {processed}/{len(codes)}", end="", flush=True)
    nd = len(trade_days)
    print(f"\n评估完成，用时 {time.time() - t0:.0f}s   交易日 {nd}")

    def agg(rows: List[Row]) -> Dict[str, float]:
        if not rows:
            return {"n": 0}
        f5 = np.concatenate([r[2] for r in rows])
        f10 = np.concatenate([r[3] for r in rows])
        f20 = np.concatenate([r[4] for r in rows])
        mx = np.concatenate([r[5] for r in rows])
        mn = np.concatenate([r[6] for r in rows])
        return {
            "n": len(f20),
            "f5": float(np.nanmean(f5) * 100),
            "f10": float(np.nanmean(f10) * 100),
            "f20": float(np.nanmean(f20) * 100),
            "win20": float((f20 > 0).mean() * 100),
            "p5": float((f5 >= 0.05).mean() * 100),
            "p10": float((mx >= 0.10).mean() * 100),
            "p15": float((mx >= 0.15).mean() * 100),
            "mx": float(np.nanmean(mx) * 100),
            "mn": float(np.nanmean(mn) * 100),
            "med20": float(np.nanmedian(f20) * 100),
        }

    print("\n" + "=" * 108)
    print("阈值组合对比（沪深主板；f5/f10/f20 = 未来收益均值，p10/p15 = 20日内摸到 +10%/+15% 概率）")
    print("=" * 108)
    header = (f"{'变体':<20}{'信号数':>8}{'/日':>7}{'f5%':>7}{'f10%':>7}{'f20%':>7}"
              f"{'涨率20%':>8}{'摸5%':>7}{'摸10%':>7}{'摸15%':>7}{'均最高%':>8}{'均最低%':>8}")
    print(header)
    print("-" * len(header))
    for v in VARIANTS:
        name = v["name"]
        s = agg(acc[name])
        if not s["n"]:
            print(f"{name:<20}{0:>8}")
            continue
        print(f"{name:<20}{s['n']:>8}{s['n']/nd:>7.1f}{s['f5']:>7.2f}{s['f10']:>7.2f}{s['f20']:>7.2f}"
              f"{s['win20']:>8.1f}{s['p5']:>7.1f}{s['p10']:>7.1f}{s['p15']:>7.1f}"
              f"{s['mx']:>8.2f}{s['mn']:>8.2f}")
    for nm, label in (("__base_low__", "基准:低位(无粘合/拐头)"), ("__base_all__", "基准:全市场")):
        s = agg(acc[nm])
        print("-" * len(header))
        print(f"{label:<20}{s['n']:>8}{s['n']/nd:>7.1f}{s['f5']:>7.2f}{s['f10']:>7.2f}{s['f20']:>7.2f}"
              f"{s['win20']:>8.1f}{s['p5']:>7.1f}{s['p10']:>7.1f}{s['p15']:>7.1f}"
              f"{s['mx']:>8.2f}{s['mn']:>8.2f}")

    def by_year(name: str, label: str) -> None:
        rows = acc[name]
        if not rows:
            return
        print("\n" + "=" * 108)
        print(f"分年度：{label}")
        print("=" * 108)
        codes_ = np.concatenate([r[0] for r in rows])
        dates_ = np.concatenate([r[1] for r in rows])
        f5 = np.concatenate([r[2] for r in rows])
        f20 = np.concatenate([r[4] for r in rows])
        mx = np.concatenate([r[5] for r in rows])
        mn = np.concatenate([r[6] for r in rows])
        yrs = np.array([d[:4] for d in dates_])
        for y in sorted(set(yrs)):
            m = yrs == y
            print(f"  {y}: 信号 {int(m.sum()):>6}  f5 {np.nanmean(f5[m])*100:>6.2f}%  "
                  f"f20 {np.nanmean(f20[m])*100:>6.2f}%  涨率 {(f20[m]>0).mean()*100:>5.1f}%  "
                  f"摸10% {(mx[m]>=0.10).mean()*100:>5.1f}%  摸15% {(mx[m]>=0.15).mean()*100:>5.1f}%  "
                  f"均最低 {np.nanmean(mn[m])*100:>6.2f}%")

    
    by_year("DEFAULT", "DEFAULT（暴跌 + 低点新鲜 + 粘合 + 金叉）")
    by_year("__base_low__", "基准：低位（只满足暴跌+低位）")
    by_year("__base_all__", "基准：全市场")

    # ---------------- 去重口径：同股 25 个自然日内只算第一次 ----------------
    print("\n" + "=" * 108)
    print("去重（同股 25 自然日冷却，≈ 实盘每日候选数）")
    print("=" * 108)

    def dedup(rows: List[Row]) -> Dict[str, float]:
        from datetime import date as _date
        recs: List[Tuple[str, str, float, float, float, float, float]] = []
        for code_, date_, f5_, f10_, f20_, mx_, mn_ in rows:
            for i in range(len(code_)):
                recs.append((code_[i], date_[i], f5_[i], f10_[i], f20_[i], mx_[i], mn_[i]))
        recs.sort(key=lambda r: (r[0], r[1]))
        keep: List[Tuple[str, str, float, float, float, float, float]] = []
        last: Dict[str, _date] = {}
        for r in recs:
            key = r[0]
            try:
                dd = _date.fromisoformat(r[1])
            except ValueError:
                dd = None
            if key in last and dd is not None and (dd - last[key]).days <= 25:
                continue
            if dd is not None:
                last[key] = dd
            keep.append(r)
        if not keep:
            return {"n": 0}
        k = np.array([r[1] for r in keep])
        f5 = np.array([r[2] for r in keep])
        f20 = np.array([r[4] for r in keep])
        mx = np.array([r[5] for r in keep])
        mn = np.array([r[6] for r in keep])
        days = len(set(k.tolist()))
        return {
            "n": len(keep), "per_day": len(keep) / max(days, 1),
            "f5": float(np.nanmean(f5) * 100), "f20": float(np.nanmean(f20) * 100),
            "win20": float((f20 > 0).mean() * 100),
            "p10": float((mx >= 0.10).mean() * 100),
            "p15": float((mx >= 0.15).mean() * 100),
            "mn": float(np.nanmean(mn) * 100),
        }

    for v in VARIANTS:
        name = v["name"]
        s = dedup(acc[name])
        if not s.get("n"):
            continue
        print(f"  {name:<20} 信号 {s['n']:>6}  每日 {s['per_day']:>5.2f}  f5 {s['f5']:>6.2f}%  "
              f"f20 {s['f20']:>6.2f}%  涨率 {s['win20']:>5.1f}%  摸10% {s['p10']:>5.1f}%  "
              f"摸15% {s['p15']:>5.1f}%  均最低 {s['mn']:>6.2f}%")

    # ---------------- 样本自检 ----------------
    print("\n" + "=" * 108)
    print("样本自检（生产特征提取 vs 研究特征提取，以及两个用户样本是否命中）")
    print("=" * 108)
    # 两个用户样本在 2026-08/09 逐日是否命中 T2_tight
    t2 = next(v for v in VARIANTS if v["name"] == "DEFAULT")
    for code in ("605366", "002990"):
        bars = store.get_many([code], args.adjust).get(code) or []
        f = features(bars)
        if f is None:
            continue
        dates = np.array([str(b.get("date")) for b in bars])
        idx = np.arange(len(dates))
        m = (dates >= "2026-08-01") & (dates <= "2026-09-30")
        sig, base = signal_mask(f, t2, idx[m])
        hit = dates[m][sig]
        print(f"  {code} DEFAULT 命中日: {list(hit)}")

    for code, day in (("605366", "2026-09-14"), ("002990", "2026-09-14"),
                      ("605366", "2026-09-11"), ("002990", "2026-09-18"),
                      ("002990", "2026-09-24")):
        bars = store.get_many([code], args.adjust).get(code) or []
        if not bars:
            continue
        f = features(bars)
        dates = [str(b.get("date")) for b in bars]
        if day not in dates or f is None:
            print(f"  {code} {day}: 无数据")
            continue
        i = dates.index(day)
        print(f"  {code} @{day}: 离散(近10日最小) {f['spread_min10'][i]*100:.2f}%  "
              f"dd250 {f['dd250'][i]*100:.1f}%  pos250 {f['pos250'][i]:.2f}  "
              f"MA5>MA10 {f['ma5'][i] > f['ma10'][i]}  收盘≥MA5 {f['c'][i] >= f['ma5'][i]}  "
              f"HIST {f['hist'][i]:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
