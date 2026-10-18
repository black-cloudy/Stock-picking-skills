"""研究步骤 18：美人肩「事件级」成败统计（近三年全样本）。

与步骤 16/17 的区别：
  16/17 是按「决策点」统计（每天一个样本，同一次首板会重复计多次）；
  本脚本把同一次「首板 + 整理」合并成一个**事件**，只取决策窗口内
  最后一个决策点作为代表，得到干净的「成功 X 次 / 失败 Y 次」。

用法::

    python research/01_meirenjian/18_event_study.py --csv research/samples.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Any, Dict, List

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.patterns import FEATURE_KEYS, SecondBoardConfig, structure_reasons  # noqa: E402

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

EXTRA = ("decision_date", "fwd_ret", "fwd_ret_2d", "fwd_ret_3d", "fwd_ret_5d",
         "fwd_min_3d", "fwd_max_3d", "fwd_gap", "label_3d", "next_date")

CMP_KEYS = (
    "board_vol_ratio", "vol_trend", "vol_decay", "vol_last_ratio", "vol_ratio_5d",
    "hist_decline", "hist_decline_days", "hist_n", "amp_last", "avg_amp",
    "turnover_last", "turnover_cons", "turnover_board",
    "gap", "boards_60d", "ma_bull", "ma_spread", "close_vs_ma5",
    "bear_ratio", "big_loss_days", "upper_shadow_last", "close_pos_day", "price",
)


def load(path: str, since: str):
    keys = list(dict.fromkeys(list(FEATURE_KEYS) + list(EXTRA)))
    buf: Dict[str, List[float]] = {k: [] for k in keys}
    codes: List[str] = []
    boards: List[str] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            d = row.get("decision_date") or ""
            if since and d < since:
                continue
            codes.append(row["code"])
            boards.append(row.get("board_date") or "")
            for k in keys:
                v = row.get(k)
                try:
                    buf[k].append(float(v) if v not in (None, "", "None") else float("nan"))
                except ValueError:
                    buf[k].append(float("nan"))
    return ({k: np.asarray(v, float) for k, v in buf.items()},
            np.asarray(codes), np.asarray(boards))


def med(x, m):
    v = x[m]
    v = v[~np.isnan(v)]
    return float(np.median(v)) if v.size else float("nan")


def mean(x, m):
    v = x[m]
    v = v[~np.isnan(v)]
    return float(np.mean(v)) if v.size else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples.csv"))
    ap.add_argument("--since", default="2023-09-01", help="只统计近三年")
    args = ap.parse_args()
    if np is None:
        return 1

    cols, codes, boards = load(args.csv, args.since)
    n = len(codes)
    print(f"期间 {args.since} 起   决策点样本 {n:,} 个")

    # ---- 合并成事件： (code, board_date) → 取 gap 最大的决策点 ----
    last: Dict[tuple, int] = {}
    g = cols["gap"]
    for i in range(n):
        key = (codes[i], boards[i])
        j = last.get(key)
        if j is None or (not np.isnan(g[i]) and g[i] > g[j]):
            last[key] = i
    idx = np.array(sorted(last.values()))
    print(f"美人肩事件 {len(idx):,} 个（每个首板只计一次，取决策窗口内最后一天）\n")

    y = (cols["label_3d"][idx] > 0).astype(int)
    succ = idx[y == 1]
    fail = idx[y == 0]
    print("=" * 96)
    print("【结果统计】判定标准：整理结束后 3 个交易日内是否出现二次涨停")
    print("=" * 96)
    print(f"  二次涨停成功 : {len(succ):>7,} 次   ({len(succ) / len(idx):.1%})")
    print(f"  二次涨停失败 : {len(fail):>7,} 次   ({len(fail) / len(idx):.1%})")

    print()
    print(f"  {'口径':<12}{'成功组':>12}{'失败组':>12}{'全部':>12}")
    for label, key in (("次日收益", "fwd_ret"), ("次日缺口", "fwd_gap"),
                       ("3日收益", "fwd_ret_3d"), ("5日收益", "fwd_ret_5d"),
                       ("3日最高", "fwd_max_3d"), ("3日最低", "fwd_min_3d")):
        a = np.nanmean(cols[key][succ]) if key in cols else np.nan
        b = np.nanmean(cols[key][fail]) if key in cols else np.nan
        c = np.nanmean(cols[key][idx]) if key in cols else np.nan
        print(f"  {label:<12}{a:>11.2%}{b:>11.2%}{c:>11.2%}")
    print()
    if "fwd_min_3d" in cols:
        v = cols["fwd_min_3d"][fail]
        v = v[~np.isnan(v)]
        if v.size:
            print("  失败组「3日最大回撤」分位："
                  + "  ".join(f"p{q}={np.percentile(v, q):+.2%}" for q in (10, 25, 50, 75)))
            print(f"  失败组中 3 日内跌破 -5% 的比例: {float((v < -0.05).mean()):.1%}"
                  f"   跌破 -3%: {float((v < -0.03).mean()):.1%}")

    # ---- 成功 vs 失败 特征对比 ----
    print()
    print("=" * 108)
    print("【成功组 vs 失败组 特征对比】（中位数；差异倍数 = 成功/失败）")
    print("=" * 108)
    print(f"{'特征':<20}{'失败组中位':>12}{'成功组中位':>12}{'差距':>12}{'方向':>14}")
    print("-" * 108)
    rows = []
    for k in CMP_KEYS:
        x = cols[k]
        fm, sm = med(x, fail), med(x, succ)
        if np.isnan(fm) or np.isnan(sm):
            continue
        ratio = (sm / fm) if fm not in (0,) else float("inf")
        rows.append((abs(np.log(ratio)) if np.isfinite(ratio) and ratio > 0 else 0, k, fm, sm, ratio))
    rows.sort(reverse=True)
    for _, k, fm, sm, ratio in rows:
        arrow = "成功组更高" if sm > fm else "成功组更低"
        rtxt = f"{ratio:.2f}x" if np.isfinite(ratio) and ratio > 0 else "-"
        print(f"{k:<20}{fm:>12.3f}{sm:>12.3f}{rtxt:>12}{arrow:>14}")
    print("=" * 108)

    # ---- 失败组的"死亡画像" ----
    print()
    print("=" * 96)
    print("【失败组画像】六条失败信号在成功/失败组的触发比例")
    print("=" * 96)
    cfg = SecondBoardConfig()
    sig = [
        ("首板放量 > 2.2 倍", cols["board_vol_ratio"] > cfg.max_board_vol_ratio),
        ("MACD柱回落 > 30%", cols["hist_decline"] > cfg.max_hist_decline),
        ("MACD柱连衰 > 2 天", cols["hist_decline_days"] > cfg.max_hist_decline_days),
        ("整理期缩量 (decay < 0.5)", cols["vol_decay"] < cfg.min_vol_decay),
        ("决策日缩量 (< 0.7)", cols["vol_ratio_5d"] < cfg.min_vol_ratio_5d),
        ("决策日振幅 < 4%", cols["amp_last"] < cfg.min_amp_last),
    ]
    print(f"{'失败信号':<28}{'失败组触发率':>14}{'成功组触发率':>14}{'倍数':>10}")
    print("-" * 96)
    for name, cond in sig:
        fr_ = float(cond[fail].mean())
        sr_ = float(cond[succ].mean())
        print(f"{name:<28}{fr_:>13.1%}{sr_:>14.1%}{fr_ / max(sr_, 1e-9):>9.2f}x")

    print()
    print(f"{'触发信号数':<12}{'事件数':>10}{'成功数':>10}{'成功率':>10}{'次日均收益':>12}{'3日最大回撤':>13}")
    print("-" * 96)
    cnt = np.zeros(len(idx), int)
    for _, cond in sig:
        cnt += cond[idx].astype(int)
    for k in range(7):
        m = cnt == k
        if m.sum() < 10:
            continue
        sel = idx[m]
        print(f"{k:<12}{int(m.sum()):>10,}{int((cols['label_3d'][sel] > 0).sum()):>10,}"
              f"{(cols['label_3d'][sel] > 0).mean():>10.1%}"
              f"{np.nanmean(cols['fwd_ret'][sel]):>11.2%}"
              f"{np.nanmean(cols['fwd_min_3d'][sel]):>12.2%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
