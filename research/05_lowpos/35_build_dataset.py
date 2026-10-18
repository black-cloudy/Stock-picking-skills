"""研究步骤 35：把 EARLY 信号扩成「可优化数据集」。

在信号特征之外加入：
  * 20 日真实收益（次日开盘买 → 20 日后收盘，扣 0.15% 成本）
  * 流通市值（meta.json 流通股本 × 信号日收盘）
  * 20 日平均成交额（close × volume）
  * 行业（market_ctx.json code_sector）
  * 大盘环境（上证 / 中证500 / 中证1000 / 国证2000：>MA20、>MA60、20日涨幅）

输出 .data/early_dataset.pkl（pandas DataFrame），供 36_optimize_filters.py 使用。

用法::

    python research/05_lowpos/35_build_dataset.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import pickle
import sys
import time
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

spec = importlib.util.spec_from_file_location(
    "lp_port", os.path.join(_p, "research", "05_lowpos", "31_lowpos_portfolio.py"))
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)  # type: ignore[union-attr]

ROOT = _p
OUT = os.path.join(ROOT, ".data", "early_dataset.pkl")
EXTRA = os.path.join(ROOT, ".data", "signal_extra.pkl")

INDEX_FILES = {
    "sh": "index_sh000001.json",
    "zz500": "index_sh000905.json",
    "zz1000": "index_sh000852.json",
    "gz2000": "index_sz399303.json",
}


def load_index(name: str) -> Dict[str, Any]:
    rows = json.load(open(os.path.join(ROOT, ".data", name), encoding="utf-8"))
    rows = sorted(rows, key=lambda r: r["day"])
    dates = [r["day"] for r in rows]
    c = np.array([float(r["close"]) for r in rows])

    def ma(w: int) -> np.ndarray:
        out = np.full(len(c), np.nan)
        if len(c) >= w:
            out[w - 1:] = np.convolve(c, np.ones(w), "valid") / w
        return out

    m20, m60 = ma(20), ma(60)
    ret20 = np.full(len(c), np.nan)
    ret20[20:] = c[20:] / c[:-20] - 1
    slope20 = np.full(len(c), np.nan)
    slope20[5:] = m20[5:] / m20[:-5] - 1
    idx = {d: i for i, d in enumerate(dates)}
    return {"idx": idx, "gt20": c > m20, "gt60": c > m60,
            "ret20": ret20, "slope20": slope20}


def main() -> int:
    signals = [s for s in pickle.load(open(lp.SIG, "rb")) if s["kind"] == "EARLY"]
    meta = json.load(open(os.path.join(ROOT, ".data", "meta.json"), encoding="utf-8"))
    sector = json.load(open(os.path.join(ROOT, ".data", "market_ctx.json"),
                            encoding="utf-8"))["code_sector"]
    idx = {k: load_index(v) for k, v in INDEX_FILES.items()}
    book = lp.PriceBook(lp.RAW)

    rows: List[Dict[str, Any]] = []
    extra: Dict[Any, Any] = {}
    t0 = time.time()
    for n, s in enumerate(signals):
        name = (s.get("name") or "").upper().replace(" ", "")
        if "ST" in name or "退" in name or s["close"] < 2.0:
            continue
        p = book.get(s["code"])
        if not p:
            continue
        i = p["idx"].get(s["date"])
        if i is None:
            continue
        # 20 日平均成交额 + 流通市值（所有信号都存，包含最后 20 天）
        lo = max(0, i - 19)
        amt = float(np.mean(p["close"][lo:i + 1] * p["vol"][lo:i + 1])) / 1e8
        mcap = float((meta.get(s["code"]) or {}).get("float_shares") or 0) * s["close"] / 1e8
        extra[(s["code"], s["date"])] = (mcap, amt)
        if i + 21 >= len(p["dates"]):
            continue
        entry = p["open"][i + 1]
        prev = p["close"][i]
        if not entry or (prev and entry >= prev * 1.095):
            continue
        f20 = p["close"][i + 21] / entry - 1 - lp.FEE_BUY - lp.FEE_SELL
        rec = dict(s)
        rec.update({
            "f20": f20,
            "year": s["date"][:4],
            "mcap": mcap,
            "amount20": amt,
            "sector": sector.get(s["code"], "-"),
            "price": s["close"],
        })
        for k, obj in idx.items():
            j = obj["idx"].get(s["date"])
            if j is None:
                rec[f"{k}_gt20"] = np.nan
                rec[f"{k}_gt60"] = np.nan
                rec[f"{k}_ret20"] = np.nan
                rec[f"{k}_slope20"] = np.nan
            else:
                rec[f"{k}_gt20"] = bool(obj["gt20"][j])
                rec[f"{k}_gt60"] = bool(obj["gt60"][j])
                rec[f"{k}_ret20"] = float(obj["ret20"][j])
                rec[f"{k}_slope20"] = float(obj["slope20"][j])
        rows.append(rec)
        if (n + 1) % 20000 == 0:
            print(f"  {n+1}/{len(signals)}  {time.time()-t0:.0f}s")

    df = pd.DataFrame(rows)
    df.to_pickle(OUT)
    with open(EXTRA, "wb") as fh:
        pickle.dump(extra, fh)
    print(f"市值/成交额映射 {len(extra):,} 条 → {EXTRA}")
    print(f"数据集 {len(df):,} 行 × {len(df.columns)} 列 → {OUT}  {time.time()-t0:.0f}s")
    print(df[["date", "f20", "mcap", "amount20", "sh_gt20", "zz500_gt20"]].head(3))
    print("\n分年份：")
    print(df.groupby("year")["f20"].agg(["count", "mean", "median"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
