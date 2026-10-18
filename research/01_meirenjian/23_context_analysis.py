"""研究步骤 23：大盘 / 板块上下文特征的区分度与规则。

数据：2019-2026 深交所 1275 只，166 万决策点 / 22.2 万锚点事件。

用法::

    python research/01_meirenjian/23_context_analysis.py --csv research/samples6ctx.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Dict, List

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

KEYS = [
    "code", "board_date", "decision_date", "label_3d", "anchor_is_lu", "gap", "range_pct",
    "close_pos", "week_ma_bull", "week_hist_n", "week_ret_4w", "turnover60",
    "hist_decline", "vol_trend", "cost_std60", "chip_pct60",
    # 上下文
    "mkt_ret_5d", "mkt_ret_20d", "mkt_dev_ma20", "mkt_hist_n", "mkt_week_hist",
    "mkt_vol_ratio", "mkt_breadth",
    "sec_ret_5d", "sec_ret_20d", "sec_dev_ma20", "sec_hist_n", "sec_rank_5d",
    "fwd_ret", "fwd_min_3d",
]
CTX = ("mkt_ret_5d", "mkt_ret_20d", "mkt_dev_ma20", "mkt_hist_n", "mkt_week_hist",
       "mkt_vol_ratio", "mkt_breadth", "sec_ret_5d", "sec_ret_20d", "sec_dev_ma20",
       "sec_hist_n", "sec_rank_5d")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples6ctx.csv"))
    args = ap.parse_args()
    if np is None:
        return 1

    best: Dict[tuple, list] = {}
    n = 0
    with open(args.csv, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        hdr = next(rd)
        ix = {k: i for i, k in enumerate(hdr)}
        global KEYS
        miss = [k for k in KEYS if k not in ix]
        if miss:
            print(f"（跳过缺列: {miss}）")
            KEYS = [k for k in KEYS if k in ix]
        cols = [ix[k] for k in KEYS]
        for r in rd:
            key = (r[ix["code"]], r[ix["board_date"]])
            try:
                g = float(r[ix["gap"]])
            except ValueError:
                continue
            n += 1
            cur = best.get(key)
            if cur is None or g > cur[0]:
                best[key] = (g, [r[c] for c in cols])
    print(f"决策点 {n:,}  →  事件 {len(best):,}\n")

    def arr(k: str) -> np.ndarray:
        j = KEYS.index(k)
        out = []
        for _, row in best.values():
            v = row[j]
            try:
                out.append(float(v) if v not in ("", "None") else np.nan)
            except ValueError:
                out.append(np.nan)
        return np.asarray(out, float)

    A = {k: arr(k) for k in KEYS}
    S = A["label_3d"] > 0
    lu = A["anchor_is_lu"] > 0
    print(f"整体成功率 {S.mean():.2%}   涨停锚点 {S[lu].mean():.2%}   温和锚点 {S[~lu].mean():.2%}\n")

    print("=" * 108)
    print("【大盘 / 板块特征 成功 vs 失败】(中位数)")
    print("=" * 108)
    print(f"{'特征':<18}{'失败组':>11}{'成功组':>11}{'差距':>9}{'方向':>14}"
          f"{'涨停锚点AUC':>13}{'温和锚点AUC':>13}")
    print("-" * 108)

    def auc(x, y):
        m = ~np.isnan(x) & ~np.isnan(y)
        xx, yy = x[m], y[m]
        n1 = int(yy.sum()); n0 = len(yy) - n1
        if n1 == 0 or n0 == 0:
            return 0.5
        o = np.argsort(xx, kind="mergesort")
        rk = np.empty(len(xx), float); rk[o] = np.arange(1, len(xx) + 1, dtype=float)
        return float((rk[yy == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))

    yl = lu.astype(int); ym = (~lu).astype(int)
    rows = []
    for k in CTX:
        x = A[k]
        fm = np.nanmedian(x[~S]); sm = np.nanmedian(x[S])
        if np.isnan(fm) or np.isnan(sm):
            continue
        a1 = auc(x, yl); a2 = auc(x, ym)
        rows.append((max(abs(a1 - .5), abs(a2 - .5)), k, fm, sm, a1, a2))
    for _, k, fm, sm, a1, a2 in sorted(rows, reverse=True):
        print(f"{k:<18}{fm:>11.4f}{sm:>11.4f}"
              f"{'成功更高' if sm > fm else '成功更低':>18}{'':>10}"
              f"{a1:>13.3f}{a2:>13.3f}")
    print("=" * 108)

    # ---- 分档 ----
    print("\n【大盘/板块特征分档 → 成功率】(全部锚点，基准 %.1f%%)" % (S.mean() * 100))
    for k in CTX:
        x = A[k]
        v = x[~np.isnan(x)]
        if v.size < 2000:
            continue
        e = np.percentile(v, [0, 20, 40, 60, 80, 100])
        line = f"  {k:<16}"
        for i in range(5):
            m = (x >= e[i]) & ((x <= e[i + 1]) if i == 4 else (x < e[i + 1]))
            line += f"{S[m].mean():>9.1%}" if m.sum() >= 200 else f"{'-':>9}"
        print(line + f"   区间 {e[0]:+.2f}~{e[-1]:+.2f}")

    # ---- 规则 ----
    print()
    print("=" * 112)
    print("【加入大盘/板块后的规则】")
    print("=" * 112)
    def ev(name, conds):
        m = np.ones(len(S), bool)
        for k, op, t in conds:
            x = A[k]
            m &= (x >= t) if op == ">=" else (x <= t)
        m &= ~np.isnan(A["fwd_ret"])
        if m.sum() < 30:
            print(f"{name:<48}{int(m.sum()):>9}   样本不足")
            return
        r = S[m].mean()
        print(f"{name:<48}{int(m.sum()):>9,}{r:>9.2%}{r / S.mean():>8.2f}x"
              f"{np.nanmean(A['fwd_ret'][m]):>10.2%}{np.nanmean(A['fwd_min_3d'][m]):>10.2%}")

    print(f"{'规则':<48}{'事件数':>9}{'成功率':>9}{'提升':>8}{'次日':>10}{'3日回撤':>10}")
    BASE = [("gap", ">=", 3), ("gap", "<=", 10), ("range_pct", "<=", 0.12)]
    ev("A 形态基准", BASE)
    ev("A + 大盘5日涨≥0", BASE + [("mkt_ret_5d", ">=", 0)])
    ev("A + 大盘在MA20上", BASE + [("mkt_dev_ma20", ">=", 0)])
    ev("A + 大盘MACD柱>0", BASE + [("mkt_hist_n", ">=", 0)])
    ev("A + 大盘周线柱>0", BASE + [("mkt_week_hist", ">=", 0)])
    ev("A + 涨家数占比≥50%", BASE + [("mkt_breadth", ">=", 0.5)])
    ev("A + 板块5日涨≥0", BASE + [("sec_ret_5d", ">=", 0)])
    ev("A + 板块20日涨≥0", BASE + [("sec_ret_20d", ">=", 0)])
    ev("A + 板块MACD柱>0", BASE + [("sec_hist_n", ">=", 0)])
    ev("A + 板块5日排名前30%", BASE + [("sec_rank_5d", ">=", 0.7)])
    print()
    ev("★ 大盘+板块 都强", BASE + [("mkt_ret_5d", ">=", 0), ("mkt_breadth", ">=", 0.5),
                                   ("sec_ret_5d", ">=", 0), ("sec_hist_n", ">=", 0)])
    ev("★★ 大盘+板块+周线+近4周", BASE + [("mkt_breadth", ">=", 0.5), ("sec_hist_n", ">=", 0),
                                          ("week_ma_bull", ">=", 1), ("week_ret_4w", ">=", 5)])
    ev("★★★ 上面再加原六条规避", BASE + [("mkt_breadth", ">=", 0.5), ("sec_hist_n", ">=", 0),
                                          ("week_ma_bull", ">=", 1), ("week_ret_4w", ">=", 5),
                                          ("hist_decline", "<=", 0.35), ("vol_trend", ">=", 0.6)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
