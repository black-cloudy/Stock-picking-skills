"""研究步骤 7：标定「打分 → 次日二次涨停概率」，并验证结构过滤器的效果。

用法::

    python research/01_meirenjian/07_calibrate.py --csv research/samples.csv
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

from stock_agent.screener.patterns import (  # noqa: E402
    SCORE_SPEC,
    SecondBoardConfig,
    compute_score,
    structure_reasons,
)

ROOT = _p
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

# 统一从 CSV 读取全部数值列，避免漏列导致结构过滤全部失败
NEED: List[str] = []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "research", "samples.csv"))
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--split", default="2025-01-01")
    ap.add_argument("--market", default="all", choices=["all", "sz", "sh"],
                    help="只统计某市场的股票（sz=深交所）")
    args = ap.parse_args()
    if np is None:
        return 1

    cfg = SecondBoardConfig()
    if args.market == "sz":
        def _keep(code: str) -> bool:
            return code.startswith(("000", "001", "002", "003", "300", "301"))
    elif args.market == "sh":
        def _keep(code: str) -> bool:
            return code.startswith(("600", "601", "603", "605", "688", "689"))
    else:
        def _keep(code: str) -> bool:
            return True

    rows: List[Dict[str, Any]] = []
    with open(args.csv, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            d = r.get("decision_date") or ""
            if d < args.since or not _keep(r.get("code") or ""):
                continue
            rows.append(r)
    print(f"载入 {len(rows)} 条样本（{args.since} 之后, market={args.market}）")
    print(f"生效阈值: close_band=±{cfg.close_band:.0%}  gap={cfg.min_gap}~{cfg.max_gap}  "
          f"boards_60d>={cfg.min_boards_60d}  vol_trend>={cfg.min_vol_trend}")

    feats: Dict[str, Any] = {}
    scores, labels, dates, passed = [], [], [], []
    score_keys = [k for k, _, _, _ in SCORE_SPEC]
    for r in rows:
        feats.clear()
        for k, v in r.items():
            if k in ("code", "name", "board_date", "decision_date", "next_date"):
                continue
            try:
                feats[k] = float(v) if v not in (None, "", "None") else None
            except (ValueError, TypeError):
                feats[k] = None
        for k in score_keys:          # 打分需要的列一定要在
            feats.setdefault(k, None)
        ok = not structure_reasons(feats, cfg)
        passed.append(ok)
        scores.append(compute_score(feats))
        labels.append(int(float(r.get("label_next") or 0)))
        dates.append(r.get("decision_date") or "")

    sc = np.asarray(scores)
    y = np.asarray(labels)
    dt = np.asarray(dates)
    ps = np.asarray(passed, dtype=bool)
    test = dt >= args.split
    base_test = float(y[test].mean())

    print(f"结构过滤通过率: {ps.mean():.2%}")
    print(f"测试集基准命中率: {base_test:.2%}\n")

    sel = ps & test
    days = len(np.unique(dt[sel]))
    print(f"测试集通过结构过滤的样本 {int(sel.sum())}，覆盖 {days} 个交易日，"
          f"平均每日 {sel.sum() / max(days, 1):.1f} 只\n")

    print("=" * 88)
    print("打分十分位 → 次日涨停概率（测试集，已通过结构过滤）")
    print("=" * 88)
    print(f"{'分位':<6}{'样本':>8}{'打分区间':>20}{'涨停率':>10}{'提升':>8}{'次日均收益':>12}{'开→收':>10}")
    edges = np.percentile(sc[sel], np.arange(0, 101, 10))
    table: List[tuple] = []
    for i in range(10):
        lo, hi = edges[i], edges[i + 1]
        m = sel & (sc >= lo) & ((sc <= hi) if i == 9 else (sc < hi))
        if m.sum() == 0:
            continue
        pr = float(y[m].mean())
        table.append((float(lo), pr))
        print(f"D{i + 1:<5}{int(m.sum()):>8}{f'{lo:.3f}~{hi:.3f}':>20}"
              f"{pr:>10.2%}{pr / base_test:>7.2f}x{'':>12}{'':>10}")

    print()
    print("=" * 88)
    print("打分阈值 → 命中率 / 每日候选数（可直接写入 SCORE_CALIBRATION）")
    print("=" * 88)
    print(f"{'打分 ≥':<9}{'样本':>8}{'每日候选':>10}{'涨停率':>10}{'提升':>8}")
    calib: List[tuple] = []
    for t in (0.0, 0.40, 0.44, 0.48, 0.52, 0.56, 0.60, 0.64, 0.68, 0.72, 0.78):
        m = sel & (sc >= t)
        if m.sum() < 50:
            continue
        pr = float(y[m].mean())
        d = len(np.unique(dt[m]))
        calib.append((t, pr))
        print(f"{t:<9.2f}{int(m.sum()):>8}{m.sum() / max(d, 1):>10.1f}{pr:>10.2%}{pr / base_test:>7.2f}x")

    print("\n建议写入 patterns.py 的 SCORE_CALIBRATION：")
    print("SCORE_CALIBRATION: Tuple[Tuple[float, float], ...] = (")
    for t, pr in calib:
        print(f"    ({t:.2f}, {pr:.4f}),")
    print(")")

    print("\n" + "=" * 88)
    print("分年度稳定性（结构过滤通过 + 打分≥0.52）")
    print("=" * 88)
    for yr in sorted({d[:4] for d in dt}):
        ym = np.array([d.startswith(yr) for d in dt])
        if int((ym & test).sum()) < 300:
            continue
        m = sel & ym & (sc >= 0.52)
        if m.sum() < 20:
            continue
        b = float(y[ym & test].mean())
        pr = float(y[m].mean())
        print(f"  {yr}  基准 {b:>6.2%}   候选 {int(m.sum()):>5}   涨停率 {pr:>6.2%}   提升 {pr / b:>5.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
