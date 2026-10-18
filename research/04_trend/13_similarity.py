"""研究步骤 13：以用户提供的两个样本为「模板」做相似度搜索。

模板（加速前一天）：
  002990 盛视科技 @ 2026-04-13   未来5日 +40.9%
  002655 共达电声 @ 2026-05-06   未来5日 +12.7%

用法::

    python research/04_trend/13_similarity.py --csv research/trend_samples.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Dict, List, Tuple

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.trend import TREND_FEATURE_KEYS  # noqa: E402

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

EXTRA = ("date", "fwd_ret_5d", "fwd_ret_10d", "fwd_ret_20d",
         "fwd_max_20d", "fwd_min_20d", "fwd_limit_up_20d", "code")

# 模板特征（来自 extract_trend 实测）
TEMPLATE = {
    "盛视科技": {
        "total_gain": 20.277, "up_ratio": 0.700, "max_gain": 10.031, "max_loss": -3.172,
        "max_pullback": 0.032, "ret_std": 2.880, "up_streak": 5, "ma_bull": 1,
        "ma_bull_ratio": 0.450, "ma_spread": 0.148, "bias5": 4.828, "bias10": 9.967,
        "bias20": 15.166, "dif_n": 4.319, "dea_n": 2.723, "hist_n": 3.191,
        "hist_slope": 0.462, "hist_pos_ratio": 1.000, "macd_days_since_cross": 21,
        "vol_ratio": 3.269, "vol_trend": 1.736, "vol_updown": 1.452,
        "ret_60d": 15.860, "ret_120d": 27.574, "close_vs_hh60": 0.989, "close_vs_hh120": 0.989,
    },
    "共达电声": {
        "total_gain": 73.718, "up_ratio": 0.900, "max_gain": 10.007, "max_loss": -1.717,
        "max_pullback": 0.017, "ret_std": 2.786, "up_streak": 9, "ma_bull": 1,
        "ma_bull_ratio": 1.000, "ma_spread": 0.358, "bias5": 9.547, "bias10": 20.937,
        "bias20": 35.638, "dif_n": 9.957, "dea_n": 7.319, "hist_n": 5.276,
        "hist_slope": 0.967, "hist_pos_ratio": 1.000, "macd_days_since_cross": 19,
        "vol_ratio": 1.639, "vol_trend": 1.151, "vol_updown": 1.203,
        "ret_60d": 150.154, "ret_120d": 157.891, "close_vs_hh60": 0.978, "close_vs_hh120": 0.978,
    },
}

# 用于相似度匹配的特征（剔除与市场环境强相关的量纲特征）
MATCH_KEYS = (
    "up_ratio", "max_pullback", "ret_std", "ma_bull", "ma_bull_ratio", "ma_spread",
    "bias5", "bias10", "hist_n", "hist_slope", "hist_pos_ratio",
    "vol_updown", "vol_trend", "close_vs_hh60",
)


def load(path: str, since: str):
    keys = list(dict.fromkeys(list(TREND_FEATURE_KEYS) + list(EXTRA)))
    buf: Dict[str, List[float]] = {k: [] for k in keys}
    dates: List[str] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            d = row.get("date") or ""
            if since and d < since:
                continue
            dates.append(d)
            for k in keys:
                v = row.get(k)
                try:
                    buf[k].append(float(v) if v not in (None, "", "None") else float("nan"))
                except ValueError:
                    buf[k].append(float("nan"))
    return {k: np.asarray(v, float) for k, v in buf.items()}, np.asarray(dates)


def stats(cols, m) -> Tuple[int, float, float, float, float, float]:
    m = m & ~np.isnan(cols["fwd_ret_20d"])
    n = int(m.sum())
    if n == 0:
        return 0, 0, 0, 0, 0, 0
    return (
        n,
        float(np.nanmean(cols["fwd_ret_5d"][m])),
        float(np.nanmean(cols["fwd_ret_20d"][m])),
        float(np.nanmean((cols["fwd_ret_20d"][m] > 0).astype(float))),
        float(np.nanmean((cols["fwd_max_20d"][m] >= 15).astype(float))),
        float(np.nanmean(cols["fwd_min_20d"][m])),
    )


def apply(cols, conds) -> np.ndarray:
    n = len(cols["price"])
    m = np.ones(n, bool)
    for k, op, t in conds:
        x = cols[k]
        m &= (x >= t) if op == ">=" else ((x <= t) if op == "<=" else (x > t))
    return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "trend_samples.csv"))
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--split", default="2025-01-01")
    args = ap.parse_args()
    if np is None:
        return 1

    cols, dates = load(args.csv, args.since)
    test = dates >= args.split
    n0, r5, r20, up, h15, mn = stats(cols, test)
    print(f"测试集 {n0}  基准: 5日 {r5:+.2f}%  20日 {r20:+.2f}%  上涨率 {up:.1%}  "
          f"摸+15% {h15:.1%}  最低 {mn:+.2f}%\n")

    print("=" * 112)
    print(f"{'规则（逐步贴近模板）':<56}{'样本':>7}{'5日':>9}{'20日':>9}{'上涨率':>8}{'摸+15%':>9}{'最低':>9}")
    print("=" * 112)

    # 模板共性条件（两人都满足）
    CORE = [
        ("ma_bull", ">=", 1),            # 均线多头
        ("max_pullback", "<=", 0.045),   # 回撤极小（模板 1.7%/3.2%）
        ("hist_pos_ratio", ">=", 0.85),  # MACD 红柱几乎全程
        ("close_vs_hh60", ">=", 0.95),   # 贴着 60 日新高
        ("vol_updown", ">=", 1.10),      # 涨日量 > 跌日量
        ("hist_slope", ">", 0.0),        # MACD 柱在放大
        ("bias5", ">=", 3.0),            # 短期强势
        ("ret_std", "<=", 3.6),          # 走势平滑
    ]
    tests: List[Tuple[str, list]] = [
        ("① 全部共性条件", CORE),
        ("② ① + up_ratio≥0.70（盛视）", CORE + [("up_ratio", ">=", 0.70)]),
        ("③ ① + up_ratio≥0.85（共达）", CORE + [("up_ratio", ">=", 0.85)]),
        ("④ ① + hist_pos_ratio=1.0", CORE + [("hist_pos_ratio", ">=", 0.999)]),
        ("⑤ ① + 回撤≤3%", CORE + [("max_pullback", "<=", 0.03)]),
        ("⑥ ① + bias5≥5%（两人均值）", CORE + [("bias5", ">=", 5.0)]),
        ("⑦ ① + bias10≥10%", CORE + [("bias10", ">=", 10.0)]),
        ("⑧ ① + vol_updown≥1.3", CORE + [("vol_updown", ">=", 1.30)]),
        ("⑨ ① + hist_n≥3", CORE + [("hist_n", ">=", 3.0)]),
        ("⑩ 收紧: 回撤≤3% + up≥0.75 + bias5≥5", CORE + [
            ("max_pullback", "<=", 0.03), ("up_ratio", ">=", 0.75), ("bias5", ">=", 5.0)]),
        ("⑪ 最严: +hist_pos=1 + close_vs_hh60≥0.98", CORE + [
            ("max_pullback", "<=", 0.03), ("up_ratio", ">=", 0.75), ("bias5", ">=", 5.0),
            ("hist_pos_ratio", ">=", 0.999), ("close_vs_hh60", ">=", 0.98)]),
    ]
    for name, conds in tests:
        s = stats(cols, apply(cols, conds) & test)
        if s[0] < 15:
            print(f"  {name:<56}{s[0]:>7}   样本不足")
            continue
        print(f"  {name:<56}{s[0]:>7}{s[1]:>+8.2f}%{s[2]:>+8.2f}%{s[3]:>8.1%}{s[4]:>9.1%}{s[5]:>+8.2f}%")
    print("=" * 112)

    # ---- 最近邻（马氏距离，标准化后）----
    print("\n最近邻搜索（14 维特征标准化后的欧氏距离，训练集定标）")
    train = ~test
    mean_ = {}
    std_ = {}
    for k in MATCH_KEYS:
        v = cols[k][train]
        mean_[k] = float(np.nanmean(v))
        std_[k] = float(np.nanstd(v)) or 1.0

    def z(key, arr=None):
        v = cols[key] if arr is None else arr
        return (v - mean_[key]) / std_[key]

    tpl_vecs = []
    for nm, t in TEMPLATE.items():
        tpl_vecs.append(np.array([(t[k] - mean_[k]) / std_[k] for k in MATCH_KEYS]))
    tpl = np.stack(tpl_vecs)

    dist = np.full(len(dates), np.inf)
    for i in range(0, len(dates), 20000):
        sl = slice(i, min(i + 20000, len(dates)))
        chunk = np.stack([z(k)[sl] for k in MATCH_KEYS], axis=1)
        d = np.min(np.linalg.norm(chunk[:, None, :] - tpl[None, :, :], axis=2), axis=1)
        dist[sl] = d

    print(f"  {'距离分位':<16}{'样本':>8}{'5日':>10}{'20日':>10}{'上涨率':>9}{'摸+15%':>9}{'最低':>10}")
    for pct in (0.1, 0.3, 0.5, 1, 2, 5, 10):
        thr = np.nanpercentile(dist[test], pct)
        m = test & (dist <= thr)
        s = stats(cols, m)
        if s[0] == 0:
            continue
        print(f"  最近 {pct:>4.1f}% ({thr:>4.2f}){s[0]:>8}{s[1]:>+9.2f}%{s[2]:>+9.2f}%"
              f"{s[3]:>9.1%}{s[4]:>9.1%}{s[5]:>+9.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
