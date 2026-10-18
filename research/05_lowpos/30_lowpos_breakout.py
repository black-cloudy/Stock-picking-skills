"""研究步骤 30：低位均线粘合的两个阶段 —— 早期拐头 vs 突破加速。

用户 2026-10 新增 6 个样本（粤宏远A / 华媒控股 / 吉大正元 / 皇台酒业 /
金种子酒 / 读者传媒）都是「低位平台 + 均线粘合 → 放量大阳/涨停突破」，
买点比 2026-09 的两个样本（宏柏新材 / 盛视科技）更靠后。

本脚本在 raw_bars_deep.db（不复权真实价格，2017-2026）上逐日提取特征：

* EARLY：原有口径（低位 + 粘合 + 刚金叉 + BIAS20 ≤ 8%）
* BREAK：新增口径（低位 + 粘合 + 已启动：大阳/涨停、MA5>MA10、BIAS20 ≤ 20%）
* COMBINED：两者并集

输出：各口径 5/10/20 日收益、摸 +10%/+15%、回撤、分年度、去重；
并把 COMBINED 信号表存成 .data/lowpos_signals.pkl 供组合回测使用。

用法::

    python research/05_lowpos/30_lowpos_breakout.py
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

ROOT = _p
RAW = os.path.join(ROOT, ".data", "raw_bars_deep.db")
OUT_PKL = os.path.join(ROOT, ".data", "lowpos_signals.pkl")

Row = Tuple[np.ndarray, ...]


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
    if len(x) >= w:
        out[w - 1:] = np.lib.stride_tricks.sliding_window_view(x, w).min(axis=1)
    return out


def roll_max(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if len(x) >= w:
        out[w - 1:] = np.lib.stride_tricks.sliding_window_view(x, w).max(axis=1)
    return out


def features(bars: List[Dict[str, Any]]) -> Optional[Dict[str, np.ndarray]]:
    try:
        o = np.array([float(b["open"]) for b in bars])
        c = np.array([float(b["close"]) for b in bars])
        h = np.array([float(b["high"]) for b in bars])
        l = np.array([float(b["low"]) for b in bars])
        v = np.array([float(b.get("volume") or 0.0) for b in bars])
    except (TypeError, ValueError, KeyError):
        return None
    n = len(c)
    if n < 260:
        return None

    ma5, ma10, ma15 = ma_np(c, 5), ma_np(c, 10), ma_np(c, 15)
    ma20, ma30 = ma_np(c, 20), ma_np(c, 30)
    m4 = np.vstack([ma5, ma10, ma15, ma20])
    spread = (m4.max(0) - m4.min(0)) / (m4.mean(0) + 1e-12)
    spread_min10 = roll_min(spread, 10)
    spread_min20 = roll_min(spread, 20)

    hh250, ll250 = roll_max(h, 250), roll_min(l, 250)
    hh120, ll120 = roll_max(h, 120), roll_min(l, 120)
    dd250 = (hh250 - ll250) / (hh250 + 1e-12)
    dd120 = (hh120 - ll120) / (hh120 + 1e-12)
    pos250 = (c - ll250) / (hh250 - ll250 + 1e-12)
    vs_hh250 = c / (hh250 + 1e-12)

    low60 = np.full(n, np.nan)
    if n >= 60:
        wl = np.lib.stride_tricks.sliding_window_view(l, 60)
        low60[59:] = 59 - wl.argmin(axis=1)

    dif, dea, hist = macd_np(c)
    hist_prev = np.roll(hist, 1)
    hist_prev[0] = hist[0]
    hist_slope = hist - hist_prev
    hist_slope3 = hist - np.roll(hist, 3)
    hist_slope3[:3] = 0

    chg1 = np.zeros(n)
    chg1[1:] = c[1:] / (c[:-1] + 1e-12) - 1.0
    chg3 = np.zeros(n)
    chg3[3:] = c[3:] / (c[:-3] + 1e-12) - 1.0
    ret20 = np.zeros(n)
    ret20[20:] = c[20:] / (c[:-20] + 1e-12) - 1.0

    vol5 = ma_np(v, 5)
    vol20 = ma_np(v, 20)
    vol_today = np.full(n, np.nan)
    # 今天量 / 前 5 日均量（不含今天）
    for i in range(5, n):
        base = v[i - 5:i].mean()
        vol_today[i] = v[i] / base if base else np.nan

    bias5 = c / (ma5 + 1e-12) - 1.0
    bias20 = c / (ma20 + 1e-12) - 1.0
    close_break20 = np.zeros(n, dtype=bool)
    high_break20 = np.zeros(n, dtype=bool)
    if n > 21:
        close_break20[20:] = c[20:] > np.lib.stride_tricks.sliding_window_view(c, 20)[:-1].max(axis=1)
        high_break20[20:] = c[20:] > np.lib.stride_tricks.sliding_window_view(h, 20)[:-1].max(axis=1)

    return {
        "o": o, "c": c, "h": h, "l": l, "v": v,
        "ma5": ma5, "ma10": ma10, "ma15": ma15, "ma20": ma20, "ma30": ma30,
        "spread": spread, "spread_min10": spread_min10, "spread_min20": spread_min20,
        "hh250": hh250, "ll250": ll250, "hh120": hh120, "ll120": ll120,
        "dd250": dd250, "dd120": dd120, "pos250": pos250, "vs_hh250": vs_hh250,
        "low_age60": low60,
        "dif": dif, "dea": dea, "hist": hist, "hist_slope": hist_slope,
        "hist_slope3": hist_slope3,
        "chg1": chg1, "chg3": chg3, "ret20": ret20,
        "vol5": vol5, "vol20": vol20, "vol_today": vol_today,
        "bias5": bias5, "bias20": bias20,
        "close_break20": close_break20, "high_break20": high_break20,
    }


# ---------------------------------------------------------------- 信号定义

EARLY = dict(dd250=0.30, pos250=0.45, vs_hh250=0.75, dd120=0.35, low_age=45,
             spread10=0.025, spread_now=0.040, cross_days=5, cross_spread=0.040,
             bias_lo=-0.06, bias_hi=0.08, hist_min=0.0)

BREAK = dict(dd250=0.30, pos250=0.65, vs_hh250=0.87,
             spread20=0.030, chg1=0.04, chg3=0.08,
             bias_hi=0.20, vol_min=0.70)


def masks(f: Dict[str, np.ndarray], idx: np.ndarray) -> Dict[str, np.ndarray]:
    g = lambda k: f[k][idx]
    out: Dict[str, np.ndarray] = {}

    # ---- EARLY ----
    base = ((g("dd250") >= EARLY["dd250"]) & (g("pos250") <= EARLY["pos250"])
            & (g("vs_hh250") <= EARLY["vs_hh250"])
            & (g("dd120") >= EARLY["dd120"]) & (g("low_age60") <= EARLY["low_age"]))
    conv = g("spread_min10") <= EARLY["spread10"]
    cross = np.zeros(len(idx), dtype=bool)
    ma5, ma10 = g("ma5"), g("ma10")
    for k in range(len(idx)):
        if k < 1 or np.isnan(ma5[k]) or np.isnan(ma10[k]):
            continue
        for j in range(0, EARLY["cross_days"] + 1):
            if k - j - 1 < 0:
                break
            if (ma5[k - j] > ma10[k - j] and ma5[k - j - 1] <= ma10[k - j - 1]
                    and (f["spread"][idx[k - j]] <= EARLY["cross_spread"])):
                cross[k] = True
                break
    early = (base & conv & cross & (g("c") >= ma5 * 0.99)
             & (g("hist") >= EARLY["hist_min"])
             & (g("bias20") >= EARLY["bias_lo"]) & (g("bias20") <= EARLY["bias_hi"]))
    out["EARLY"] = early

    # ---- BREAK ----
    bbase = ((g("dd250") >= BREAK["dd250"]) & (g("pos250") <= BREAK["pos250"])
             & (g("vs_hh250") <= BREAK["vs_hh250"])
             & (g("spread_min20") <= BREAK["spread20"]))
    mom = (g("chg1") >= BREAK["chg1"]) | (g("chg3") >= BREAK["chg3"])
    vol = g("vol_today") >= BREAK["vol_min"]
    bull = (g("ma5") > g("ma10")) & (g("ma10") >= g("ma20") * 0.990)
    brk = (bbase & mom & vol & bull & (g("c") >= g("ma5") * 0.99)
           & (g("hist") >= -0.005) & (g("bias20") <= BREAK["bias_hi"]))
    out["BREAK"] = brk

    # 变体
    out["B_NO_VOL"] = bbase & mom & bull & (g("c") >= ma5 * 0.99) & (g("hist") >= -0.005) & (g("bias20") <= 0.20)
    out["B_BREAK20"] = brk & g("close_break20")
    out["B_LIMIT"] = brk & (g("chg1") >= 0.095)
    out["B_VOL12"] = brk & (g("vol_today") >= 1.2)
    out["B_POS55"] = brk & (g("pos250") <= 0.55)
    out["B_BIAS15"] = brk & (g("bias20") <= 0.15)
    out["B_SPREAD15"] = brk & (g("spread_min20") <= 0.015)
    out["B_MAFAN"] = brk & (g("ma5") > g("ma10")) & (g("ma10") > g("ma20"))
    out["COMBINED"] = early | brk

    for k in out:
        out[k] = out[k].astype(bool)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-04")
    ap.add_argument("--end", default="2026-10-16")
    ap.add_argument("--batch", type=int, default=300)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    import sqlite3
    con = sqlite3.connect(f"file:{RAW}?mode=ro", uri=True)
    con.row_factory = None
    names = dict(con.execute("SELECT code, name FROM names").fetchall())
    codes = [r[0] for r in con.execute("SELECT DISTINCT code FROM bars ORDER BY code")]
    if args.limit:
        codes = codes[: args.limit]

    acc: Dict[str, List[Row]] = {k: [] for k in
                                 ("EARLY", "BREAK", "B_NO_VOL", "B_BREAK20", "B_LIMIT",
                                  "B_VOL12", "B_POS55", "B_BIAS15", "B_SPREAD15",
                                  "B_MAFAN", "COMBINED")}
    rows_pkl: List[Dict[str, Any]] = []
    trade_days: set = set()
    t0 = time.time()
    done = 0

    for i0 in range(0, len(codes), args.batch):
        batch = codes[i0:i0 + args.batch]
        marks = ",".join("?" * len(batch))
        data: Dict[str, List[Dict[str, Any]]] = {c: [] for c in batch}
        for code, date, o, h, l, c, v in con.execute(
                f"SELECT code,date,open,high,low,close,volume FROM bars "
                f"WHERE code IN ({marks}) AND date>='2016-01-01' ORDER BY code,date", batch):
            data[code].append({"date": date, "open": o, "high": h, "low": l,
                               "close": c, "volume": v})
        for code in batch:
            bars = data[code]
            if len(bars) < 260:
                continue
            f = features(bars)
            if f is None:
                continue
            dates = np.array([b["date"] for b in bars])
            n = len(bars)
            i0d = max(int(np.searchsorted(dates, args.start)), 249)
            if n - 1 <= i0d:
                continue
            rr_all = np.arange(i0d, n)
            ok = (~np.isnan(f["dd250"][rr_all])) & (~np.isnan(f["spread_min10"][rr_all]))
            rr_all = rr_all[ok]
            if not len(rr_all):
                continue

            fwd5 = np.full(len(rr_all), np.nan)
            fwd10 = np.full(len(rr_all), np.nan)
            fwd20 = np.full(len(rr_all), np.nan)
            mx = np.full(len(rr_all), np.nan)
            mn = np.full(len(rr_all), np.nan)
            for k, i in enumerate(rr_all):
                fwd5[k] = f["c"][i + 5] / f["c"][i] - 1 if i + 5 < n else np.nan
                fwd10[k] = f["c"][i + 10] / f["c"][i] - 1 if i + 10 < n else np.nan
                if i + 20 < n:
                    fwd20[k] = f["c"][i + 20] / f["c"][i] - 1
                    mx[k] = f["h"][i + 1:i + 21].max() / f["c"][i] - 1
                    mn[k] = f["l"][i + 1:i + 21].min() / f["c"][i] - 1
            has_fwd = ~np.isnan(fwd20)
            codes_k = np.array([code] * len(rr_all))
            dates_k = dates[rr_all]
            trade_days.update(dates_k[has_fwd].tolist())

            mm = masks(f, rr_all)
            for name, sig in mm.items():
                sig_f = sig & has_fwd
                if sig_f.any():
                    acc[name].append((codes_k[sig_f], dates_k[sig_f], fwd5[sig_f], fwd10[sig_f],
                                      fwd20[sig_f], mx[sig_f], mn[sig_f]))
            # 组合信号表（COMBINED），供回测（包含最后 20 天，便于持有到期末）
            sig = mm["COMBINED"]
            if sig.any():
                g = lambda k: f[k][rr_all][sig]
                for j in range(int(sig.sum())):
                    rows_pkl.append({
                        "date": str(dates_k[sig][j]), "code": str(codes_k[sig][j]),
                        "name": names.get(str(codes_k[sig][j]), ""),
                        "kind": "BREAK" if mm["BREAK"][sig][j] else "EARLY",
                        "close": float(g("c")[j]),
                        "chg1": float(g("chg1")[j]), "chg3": float(g("chg3")[j]),
                        "ret20": float(g("ret20")[j]),
                        "volR": float(g("vol_today")[j]),
                        "spread10": float(g("spread_min10")[j]),
                        "spread20": float(g("spread_min20")[j]),
                        "pos250": float(g("pos250")[j]),
                        "dd250": float(g("dd250")[j]),
                        "dd120": float(g("dd120")[j]),
                        "low_age60": float(g("low_age60")[j]),
                        "bias20": float(g("bias20")[j]),
                        "hist": float(g("hist")[j]),
                        "hist_slope3": float(g("hist_slope3")[j]),
                        "ma5": float(g("ma5")[j]), "ma10": float(g("ma10")[j]),
                        "ma20": float(g("ma20")[j]),
                        "break20": bool(g("close_break20")[j]),
                        "limit_up": bool(g("chg1")[j] >= 0.095),
                    })
        done += len(batch)
        print(f"\r  已处理 {done}/{len(codes)}", end="", flush=True)
    con.close()
    nd = len(trade_days)
    print(f"\n评估完成，用时 {time.time() - t0:.0f}s，交易日 {nd}")

    def agg(rows: List[Row]) -> Dict[str, float]:
        if not rows:
            return {"n": 0}
        f5 = np.concatenate([r[2] for r in rows])
        f10 = np.concatenate([r[3] for r in rows])
        f20 = np.concatenate([r[4] for r in rows])
        mx = np.concatenate([r[5] for r in rows])
        mn = np.concatenate([r[6] for r in rows])
        return {
            "n": len(f20), "f5": np.nanmean(f5) * 100, "f10": np.nanmean(f10) * 100,
            "f20": np.nanmean(f20) * 100, "win": (f20 > 0).mean() * 100,
            "p10": (mx >= 0.10).mean() * 100, "p15": (mx >= 0.15).mean() * 100,
            "mx": np.nanmean(mx) * 100, "mn": np.nanmean(mn) * 100,
        }

    print("\n" + "=" * 108)
    print("口径对比（不复权真实价格，2021-01 ~ 2026-10；f20 = 未来20日收益均值）")
    print("=" * 108)
    hdr = (f"{'口径':<14}{'信号':>9}{'每日':>7}{'f5%':>7}{'f10%':>7}{'f20%':>7}"
           f"{'涨率':>7}{'摸10%':>7}{'摸15%':>7}{'均最高':>8}{'均最低':>8}")
    print(hdr)
    print("-" * len(hdr))
    for k in acc:
        s = agg(acc[k])
        if not s["n"]:
            print(f"{k:<14}{0:>9}")
            continue
        print(f"{k:<14}{s['n']:>9,}{s['n']/nd:>7.1f}{s['f5']:>7.2f}{s['f10']:>7.2f}"
              f"{s['f20']:>7.2f}{s['win']:>7.1f}{s['p10']:>7.1f}{s['p15']:>7.1f}"
              f"{s['mx']:>8.2f}{s['mn']:>8.2f}")

    # 去重（同股 25 自然日）
    print("\n去重（同股 25 自然日冷却）：")
    from datetime import date as _date

    def dedup(rows: List[Row]) -> Dict[str, float]:
        recs = []
        for c_, d_, f5_, f10_, f20_, mx_, mn_ in rows:
            for i in range(len(c_)):
                recs.append((c_[i], d_[i], f5_[i], f10_[i], f20_[i], mx_[i], mn_[i]))
        recs.sort(key=lambda r: (r[0], r[1]))
        keep = []
        last: Dict[str, Any] = {}
        for r in recs:
            dd = _date.fromisoformat(r[1])
            if r[0] in last and (dd - last[r[0]]).days <= 25:
                continue
            last[r[0]] = dd
            keep.append(r)
        if not keep:
            return {"n": 0}
        f20 = np.array([r[4] for r in keep])
        mx = np.array([r[5] for r in keep])
        mn = np.array([r[6] for r in keep])
        return {"n": len(keep), "f20": f20.mean() * 100, "win": (f20 > 0).mean() * 100,
                "p10": (mx >= 0.10).mean() * 100, "p15": (mx >= 0.15).mean() * 100,
                "mn": mn.mean() * 100}

    for k in ("EARLY", "BREAK", "B_LIMIT", "B_BREAK20", "COMBINED"):
        s = dedup(acc[k])
        print(f"  {k:<12} 信号 {s['n']:>6,}  f20 {s['f20']:>6.2f}%  涨率 {s['win']:>5.1f}%  "
              f"摸15% {s['p15']:>5.1f}%  均最低 {s['mn']:>6.2f}%")

    # 分年度
    print("\n分年度 f20（DEFAULT/COMBINED）：")
    for name in ("EARLY", "BREAK", "COMBINED"):
        rows = acc[name]
        if not rows:
            continue
        years = np.concatenate([np.array([d[:4] for d in r[1]]) for r in rows])
        f20 = np.concatenate([r[4] for r in rows])
        mx = np.concatenate([r[5] for r in rows])
        print(f"  {name}:")
        for y in sorted(set(years)):
            m = years == y
            print(f"    {y}: {m.sum():>6,}  f20 {f20[m].mean()*100:>6.2f}%  "
                  f"涨率 {(f20[m]>0).mean()*100:>5.1f}%  摸15% {(mx[m]>=0.15).mean()*100:>5.1f}%")

    # 样本自检
    print("\n用户样本自检（首次命中日 / 模式）：")
    smap = {r["code"]: r for r in rows_pkl}
    checks = [("605366", "2026-09-08", "2026-09-20"),
              ("002990", "2026-09-08", "2026-09-20"),
              ("000573", "2026-10-08", "2026-10-16"),
              ("000607", "2026-10-08", "2026-10-16"),
              ("003029", "2026-10-08", "2026-10-16"),
              ("000995", "2026-10-08", "2026-10-16"),
              ("600199", "2026-10-08", "2026-10-16"),
              ("603999", "2026-10-08", "2026-10-16")]
    for code, a, b in checks:
        hits = [(r["date"], r["kind"]) for r in rows_pkl
                if r["code"] == code and a <= r["date"] <= b]
        hits.sort()
        print(f"  {code}: {hits[:6]}")

    with open(OUT_PKL, "wb") as fh:
        pickle.dump(rows_pkl, fh)
    print(f"\n信号表 → {OUT_PKL}  共 {len(rows_pkl):,} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
