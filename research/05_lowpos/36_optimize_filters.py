"""研究步骤 36：筛选参数 / 排序 / 大盘环境的训练-验证分析。

训练：2021-2023（含熊市）；验证：2024-2026（含牛市）。
目标：找「训练和验证都为正」的过滤条件与排序，而不是只在一段数据上好看。

用法::

    python research/05_lowpos/36_optimize_filters.py
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import numpy as np
import pandas as pd

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

ROOT = _p
DATA = os.path.join(ROOT, ".data", "early_dataset.pkl")
TRAIN_END = "2023-12-31"


def train_test(df: pd.DataFrame):
    return df[df["date"] <= TRAIN_END], df[df["date"] > TRAIN_END]


def fmt(x: float) -> str:
    return f"{x*100:+.2f}%"


def quantile_table(df: pd.DataFrame, col: str, qs=(0.2, 0.4, 0.6, 0.8)) -> None:
    tr, te = train_test(df)
    x = df[col]
    ok = np.isfinite(x) & np.isfinite(df["f20"])
    if ok.sum() < 1000:
        return
    edges = np.quantile(x[ok], qs)
    bins = np.digitize(x, edges)
    line = [f"{col:<12}"]
    for b in range(5):
        m_tr = (bins == b) & (df["date"] <= TRAIN_END) & np.isfinite(df["f20"])
        m_te = (bins == b) & (df["date"] > TRAIN_END) & np.isfinite(df["f20"])
        a = df.loc[m_tr, "f20"].mean() if m_tr.any() else np.nan
        b_ = df.loc[m_te, "f20"].mean() if m_te.any() else np.nan
        line.append(f"T{fmt(a)}/V{fmt(b_)}")
    print("  ".join(line))


def top5(df: pd.DataFrame, score: pd.Series, extra: str = "") -> Dict[str, float]:
    d = df.copy()
    d["_score"] = score
    d = d[np.isfinite(d["f20"])].sort_values(["date", "_score"],
                                              ascending=[True, False])
    sel = d.groupby("date", sort=False).head(5)
    tr = sel[sel["date"] <= TRAIN_END]["f20"]
    te = sel[sel["date"] > TRAIN_END]["f20"]
    return {"n": len(sel), "train": tr.mean(), "test": te.mean(),
            "tr_win": (tr > 0).mean(), "te_win": (te > 0).mean(),
            "tr_med": tr.median(), "te_med": te.median()}


def main() -> int:
    df = pd.read_pickle(DATA)
    print(f"数据集 {len(df):,} 行  {df['date'].min()} ~ {df['date'].max()}")
    tr, te = train_test(df)
    print(f"训练 {len(tr):,}  验证 {len(te):,}")

    print("\n=== 单特征 5 分位（训练/验证 20日净收益均值）===")
    for col in ("mcap", "amount20", "price", "pos250", "dd250", "dd120",
                "spread20", "spread10", "ret20", "chg1", "chg3", "bias20",
                "volR", "hist", "hist_slope3", "low_age60"):
        if col in df.columns:
            quantile_table(df, col)

    print("\n=== 大盘环境 ===")
    for col in ("sh_gt20", "sh_gt60", "zz500_gt20", "zz500_gt60",
                "zz1000_gt20", "zz1000_gt60", "gz2000_gt20", "gz2000_gt60"):
        if col not in df.columns:
            continue
        for val in (True, False):
            m = df[col] == val
            tr_m = m & (df["date"] <= TRAIN_END)
            te_m = m & (df["date"] > TRAIN_END)
            print(f"  {col}={val}: 训练 {fmt(df.loc[tr_m,'f20'].mean())} "
                  f"({tr_m.sum():>6,})  验证 {fmt(df.loc[te_m,'f20'].mean())} ({te_m.sum():>6,})")

    print("\n=== 大盘环境（指数 20 日涨幅 > 0 / 斜率 > 0）===")
    for col in ("sh_ret20", "sh_slope20", "zz500_ret20", "zz500_slope20",
                "zz1000_ret20", "gz2000_ret20"):
        if col not in df.columns:
            continue
        for val in (True, False):
            m = (df[col] > 0) == val
            tr_m = m & (df["date"] <= TRAIN_END)
            te_m = m & (df["date"] > TRAIN_END)
            print(f"  {col}>0={val}: 训练 {fmt(df.loc[tr_m,'f20'].mean())} "
                  f"({tr_m.sum():>6,})  验证 {fmt(df.loc[te_m,'f20'].mean())} ({te_m.sum():>6,})")

    print("\n=== 排序（每天前 5，训练/验证）===")
    price = df["price"]
    pos = df["pos250"]
    ret20 = df["ret20"]
    mcap = df["mcap"]
    amt = df["amount20"]
    spr = df["spread20"]
    hist = df["hist"]
    vol = df["volR"]
    scores = {
        "mild（当前）": -0.6 * pos - 0.3 * ret20 - 0.1 * price / 30,
        "mild+小市值": -0.6 * pos - 0.3 * ret20 - 0.1 * price / 30 - 0.15 * np.log1p(mcap),
        "mild+小市值+低量": -0.6 * pos - 0.3 * ret20 - 0.1 * price / 30
                          - 0.15 * np.log1p(mcap) - 0.05 * vol,
        "mild+高流动性": -0.6 * pos - 0.3 * ret20 - 0.1 * price / 30
                        + 0.05 * np.log1p(amt),
        "低位置+低价格": -0.5 * pos - 0.5 * price / 30,
        "低位置+弱20日": -0.7 * pos - 0.3 * ret20,
        "低位置+小市值": -0.6 * pos - 0.4 * np.log1p(mcap),
        "低位置+低hist": -0.6 * pos - 0.2 * ret20 - 0.2 * hist,
        "四因子": -0.45 * pos - 0.2 * ret20 - 0.2 * price / 30 - 0.15 * np.log1p(mcap),
        "反紧粘合": 0.4 * pos - 0.3 * ret20 - 0.1 * price / 30,
    }
    for name, sc in scores.items():
        r = top5(df, sc)
        print(f"  {name:<18} n={r['n']:>6,}  训练 {fmt(r['train'])} "
              f"(胜 {(r['tr_win']*100):.0f}%)  验证 {fmt(r['test'])} (胜 {(r['te_win']*100):.0f}%)")

    print("\n=== 过滤条件 + 当前排序（每天前 5）===")
    base = -0.6 * pos - 0.3 * ret20 - 0.1 * price / 30
    filters = {
        "无": np.ones(len(df), dtype=bool),
        "价格≤20": price <= 20,
        "价格≤10": price <= 10,
        "市值≤100亿": mcap <= 100,
        "市值≤50亿": mcap <= 50,
        "成交额≥1亿": amt >= 1,
        "成交额≥3亿": amt >= 3,
        "位置≤0.30": pos <= 0.30,
        "位置≤0.20": pos <= 0.20,
        "20日跌幅≤0": ret20 <= 0,
        "20日跌幅≥-15%": ret20 >= -0.15,
        "离散≤2%": spr <= 0.02,
        "hist≤0.2": hist <= 0.2,
        "价格≤20且市值≤100亿": (price <= 20) & (mcap <= 100),
        "价格≤20+成交≥1亿": (price <= 20) & (amt >= 1),
        "位置≤.3+价格≤20": (pos <= 0.30) & (price <= 20),
        "位置≤.3+成交≥1亿": (pos <= 0.30) & (amt >= 1),
        "价格≤20+市值≤100+成交≥1亿": (price <= 20) & (mcap <= 100) & (amt >= 1),
    }
    for name, m in filters.items():
        d = df[m]
        if len(d) < 100:
            print(f"  {name:<26} 样本不足")
            continue
        r = top5(d, base[m])
        print(f"  {name:<26} n={r['n']:>6,}  训练 {fmt(r['train'])} "
              f"(胜 {(r['tr_win']*100):.0f}%)  验证 {fmt(r['test'])} (胜 {(r['te_win']*100):.0f}%)")

    # 行业（只列样本最多的 15 个）
    print("\n=== 行业（当前排序前5，训练/验证）===")
    rows = []
    for sec, g in df.groupby("sector"):
        if len(g) < 800 or sec == "-":
            continue
        r = top5(g, -0.6 * g["pos250"] - 0.3 * g["ret20"] - 0.1 * g["price"] / 30)
        rows.append((sec, len(g), r["train"], r["test"], r["n"]))
    rows.sort(key=lambda x: -x[1])
    for sec, n, a, b, sn in rows[:15]:
        print(f"  {sec:<14} 样本 {n:>6,}  前5 {sn:>5,}  训练 {fmt(a)}  验证 {fmt(b)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
