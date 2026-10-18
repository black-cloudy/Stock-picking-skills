"""研究步骤 14：以用户两个样本为模板，生成「相似度打分」所需的全部参数。

输出可直接粘贴进 ``stock_agent/screener/trend.py`` 的：

* ``TREND_TEMPLATE_KEYS`` / ``MEAN`` / ``STD`` / ``VECTORS``
* ``TREND_DIST_CALIBRATION``（距离 → 未来20日摸到+15%的概率 / 20日收益 / 最大回撤）

用法::

    python research/04_trend/14_build_template.py --csv research/trend_samples.csv
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

from stock_agent.screener.trend import TREND_FEATURE_KEYS  # noqa: E402

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

# 模板：用户提供的两只票「加速前一天」（extract_trend 实测值）
TEMPLATE = {
    "002990 盛视科技 @2026-04-13": {
        "up_ratio": 0.700, "max_pullback": 0.032, "ret_std": 2.880, "ma_bull": 1,
        "ma_bull_ratio": 0.450, "ma_spread": 0.148, "bias5": 4.828, "bias10": 9.967,
        "hist_n": 3.191, "hist_slope": 0.462, "hist_pos_ratio": 1.000,
        "vol_updown": 1.452, "vol_trend": 1.736, "close_vs_hh60": 0.989,
    },
    "002655 共达电声 @2026-05-06": {
        "up_ratio": 0.900, "max_pullback": 0.017, "ret_std": 2.786, "ma_bull": 1,
        "ma_bull_ratio": 1.000, "ma_spread": 0.358, "bias5": 9.547, "bias10": 20.937,
        "hist_n": 5.276, "hist_slope": 0.967, "hist_pos_ratio": 1.000,
        "vol_updown": 1.203, "vol_trend": 1.151, "close_vs_hh60": 0.978,
    },
    # ---- 2026-09 新增：允许「中间涨停」的长趋势原型 ----
    # 600802 福建水泥：38 天 +50%，中间 2 次涨停（09-04 / 09-14），量能持续放大
    "600802 福建水泥 @2026-09-04": {
        "up_ratio": 0.5500, "max_pullback": 0.0303, "ret_std": 2.8421, "ma_bull": 1,
        "ma_bull_ratio": 0.9000, "ma_spread": 0.0772, "bias5": 6.5440, "bias10": 8.7229,
        "hist_n": 1.3630, "hist_slope": 0.3747, "hist_pos_ratio": 1.000,
        "vol_updown": 1.2066, "vol_trend": 1.7658, "close_vs_hh60": 1.0000,
    },
    "600802 福建水泥 @2026-09-16": {
        "up_ratio": 0.6000, "max_pullback": 0.0499, "ret_std": 3.7056, "ma_bull": 1,
        "ma_bull_ratio": 1.0000, "ma_spread": 0.1685, "bias5": 5.3184, "bias10": 10.8451,
        "hist_n": 3.0670, "hist_slope": 0.5280, "hist_pos_ratio": 1.000,
        "vol_updown": 1.2485, "vol_trend": 8.6234, "close_vs_hh60": 0.9556,
    },
}

MATCH_KEYS = tuple(TEMPLATE["002990 盛视科技 @2026-04-13"].keys())
_Z_CLIP = 3.0   # 与 trend.trend_distance 保持一致


def zclip(v):
    return max(-_Z_CLIP, min(_Z_CLIP, float(v)))
EXTRA = ("date", "fwd_ret_20d", "fwd_max_20d", "fwd_min_20d",
         "fwd_ret_5d", "fwd_limit_up_20d")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "trend_samples.csv"))
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--split", default="2025-01-01")
    ap.add_argument("--market", default="main", choices=["all", "main", "sz", "sh", "zb", "sszb"],
                    help="统计范围（默认 sz，因为两个模板都是深交所股票）")
    args = ap.parse_args()
    if np is None:
        return 1

    def keep(code: str) -> bool:
        if args.market == "main":
            return code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))
        if args.market == "sz":
            return code.startswith(("000", "001", "002", "003", "300", "301"))
        if args.market == "sh":
            return code.startswith(("600", "601", "603", "605", "688", "689"))
        if args.market == "zb":
            return code.startswith(("000", "001", "002", "003"))
        if args.market == "sszb":
            return code.startswith(("600", "601", "603", "605"))
        return True

    keys = list(dict.fromkeys(list(TREND_FEATURE_KEYS) + list(EXTRA)))
    buf: Dict[str, List[float]] = {k: [] for k in keys}
    dates: List[str] = []
    with open(args.csv, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            d = r.get("date") or ""
            if d < args.since or not keep(r.get("code") or ""):
                continue
            dates.append(d)
            for k in keys:
                v = r.get(k)
                try:
                    buf[k].append(float(v) if v not in (None, "", "None") else float("nan"))
                except ValueError:
                    buf[k].append(float("nan"))
    cols = {k: np.asarray(v, float) for k, v in buf.items()}
    dates = np.asarray(dates)
    train = dates < args.split
    test = ~train
    print(f"样本 {len(dates)}  训练 {train.sum()}  测试 {test.sum()}  (market={args.market})")

    mean_ = {k: float(np.nanmean(cols[k][train])) for k in MATCH_KEYS}
    std_ = {k: (float(np.nanstd(cols[k][train])) or 1.0) for k in MATCH_KEYS}

    print("\n# ================= 生成参数（粘贴到 trend.py）=================\n")
    print("TREND_TEMPLATE_KEYS: Tuple[str, ...] = (")
    for k in MATCH_KEYS:
        print(f'    "{k}",')
    print(")\n")
    print("TREND_TEMPLATE_MEAN: Dict[str, float] = {")
    for k in MATCH_KEYS:
        print(f'    "{k}": {mean_[k]:.6g},')
    print("}\n")
    print("TREND_TEMPLATE_STD: Dict[str, float] = {")
    for k in MATCH_KEYS:
        print(f'    "{k}": {std_[k]:.6g},')
    print("}\n")
    print("TREND_TEMPLATE_VECTORS: Tuple[Tuple[float, ...], ...] = (")
    for nm, t in TEMPLATE.items():
        z = ", ".join(f"{zclip((t[k] - mean_[k]) / std_[k]):.4f}" for k in MATCH_KEYS)
        print(f"    # {nm}")
        print(f"    ({z}),")
    print(")\n")

    tpl = np.stack([
        np.array([zclip((t[k] - mean_[k]) / std_[k]) for k in MATCH_KEYS])
        for t in TEMPLATE.values()
    ])
    dist = np.full(len(dates), np.inf)
    for i in range(0, len(dates), 20000):
        sl = slice(i, min(i + 20000, len(dates)))
        ch = np.stack([np.clip((cols[k][sl] - mean_[k]) / std_[k], -_Z_CLIP, _Z_CLIP)
                       for k in MATCH_KEYS], axis=1)
        dist[sl] = np.min(np.linalg.norm(ch[:, None, :] - tpl[None, :, :], axis=2), axis=1)

    # 自检：两个模板自身的距离
    for nm, t in TEMPLATE.items():
        z = np.array([zclip((t[k] - mean_[k]) / std_[k]) for k in MATCH_KEYS])
        d = float(np.min(np.linalg.norm(tpl - z, axis=1)))
        print(f"# 自检: {nm} 到模板集的距离 = {d:.4f}")

    days = len(np.unique(dates[test]))
    print(f"# 测试期 {days} 个交易日")
    print("\nTREND_DIST_CALIBRATION: Tuple[Tuple[float, float, float, float], ...] = (")
    print("    # (距离上限, 未来20日摸到+15%概率, 20日收益, 20日最大回撤)")
    for thr in (1.5, 1.7, 1.9, 2.1, 2.3, 2.5, 2.8, 3.2, 3.8, 99.0):
        m = test & (dist <= thr)
        if m.sum() < 20:
            continue
        print(f"    ({thr:.2f}, {np.nanmean((cols['fwd_max_20d'][m] >= 15).astype(float)):.4f}, "
              f"{np.nanmean(cols['fwd_ret_20d'][m]) / 100:.5f}, "
              f"{np.nanmean(cols['fwd_min_20d'][m]) / 100:.5f}),"
              f"   # n={int(m.sum())} 每日{m.sum() / days:.1f}只  "
              f"摸15%={np.nanmean((cols['fwd_max_20d'][m] >= 15).astype(float)):.1%}  "
              f"20日={np.nanmean(cols['fwd_ret_20d'][m]):+.1f}%")
    print(")")
    print(f"\n# 基准: 摸15%={np.nanmean((cols['fwd_max_20d'][test] >= 15).astype(float)):.1%} "
          f"20日={np.nanmean(cols['fwd_ret_20d'][test]):+.2f}% "
          f"最低={np.nanmean(cols['fwd_min_20d'][test]):+.2f}% "
          f"5日={np.nanmean(cols['fwd_ret_5d'][test]):+.2f}%")

    # 分年度
    print("\n# 分年度（距离 ≤ 2.30）")
    m = dist <= 2.30
    for yr in sorted({d[:4] for d in dates}):
        ym = np.array([d.startswith(yr) for d in dates])
        s = m & ym
        if int((ym & test).sum()) < 500 or s.sum() < 8:
            continue
        print(f"#   {yr}  入选 {int(s.sum()):>4}  20日 {np.nanmean(cols['fwd_ret_20d'][s]):>+6.2f}%  "
              f"摸15% {np.nanmean((cols['fwd_max_20d'][s] >= 15).astype(float)):>5.1%}  "
              f"基准20日 {np.nanmean(cols['fwd_ret_20d'][ym & test]):>+6.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
