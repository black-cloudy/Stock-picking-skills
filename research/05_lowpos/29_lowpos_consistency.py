"""研究步骤 29：低位均线粘合特征一致性校验。

对比两条路径对同一只股票同一天的 31 个特征：

* 研究路径：``research/05_lowpos/28_lowpos_study.py: features()``（numpy 向量化）
* 生产路径：``stock_agent.screener.lowpos: extract_lowpos()``（纯 Python）

两条路径必须一致，否则研究标定的阈值不能迁移到生产。

用法::

    python research/05_lowpos/29_lowpos_consistency.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Dict, List

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.lowpos import LOWPOS_FEATURE_KEYS, LowPosConfig, extract_lowpos  # noqa: E402
from stock_agent.screener.patterns import StockContext  # noqa: E402
from stock_agent.screener.scanner import default_store_path  # noqa: E402
from stock_agent.screener.store import ScreenerStore  # noqa: E402

ROOT = _p


def load_study():
    spec = importlib.util.spec_from_file_location(
        "lowpos_study", os.path.join(ROOT, "research", "05_lowpos", "28_lowpos_study.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# 生产特征名 → 研究特征名
NAME_MAP = {
    "spread_now": "spread4",
    "spread_min5": "spread_min5",
    "spread_min10": "spread_min10",
    "macd_dif": "dif", "macd_dea": "dea", "macd_hist": "hist",
    "vol_ratio": "vol_ratio", "ret20": "ret20", "ret60": "ret60",
}


def main() -> int:
    study = load_study()
    store = ScreenerStore(default_store_path())
    cfg = LowPosConfig()

    codes: List[str] = []
    with store._connect() as conn:
        conn.row_factory = None
        codes = [
            r[0] for r in conn.execute(
                "SELECT code FROM fetch_state WHERE adjust='qfq' AND bars>=400 "
                "ORDER BY code LIMIT 60"
            )
        ]

    worst: Dict[str, float] = {}
    n_checked = 0
    for code in codes:
        bars = store.get_many([code], "qfq").get(code) or []
        if len(bars) < cfg.min_bars:
            continue
        rf = study.features(bars)
        if rf is None:
            continue
        ctx = StockContext(bars, code, "")
        d = ctx.n - 1
        pf = extract_lowpos(ctx, d, cfg)   # 用完整 bars，与研究的窗口一致
        if pf is None:
            print(f"  {code}: 生产特征为空（跳过）")
            continue
        n_checked += 1
        for pk in LOWPOS_FEATURE_KEYS:
            rk = NAME_MAP.get(pk, pk)
            rv = rf.get(rk)
            pv = pf.get(pk)
            if rv is None or pv is None:
                continue
            if hasattr(rv, "__len__"):
                rv = rv[-1]
            # 单位换算：研究用小数，生产用百分数
            if rk in ("bias5", "bias20", "ret20", "ret60"):
                pv = pv / 100.0
            diff = abs(pv - float(rv)) / (abs(float(rv)) + 1e-9)
            worst[pk] = max(worst.get(pk, 0.0), diff)

    print(f"一致性校验：{n_checked} 只股票，{len(worst)} 个特征")
    bad = sorted(((k, v) for k, v in worst.items() if v > 0.02), key=lambda x: -x[1])
    if not bad:
        print("✅ 全部特征相对误差 ≤ 2%（生产与研究口径一致）")
    else:
        print("⚠️ 以下特征相对误差 > 2%：")
        for k, v in bad:
            print(f"    {k:<20} {v*100:.2f}%")
    top = sorted(worst.items(), key=lambda x: -x[1])[:8]
    print("  最大误差前 8：")
    for k, v in top:
        print(f"    {k:<20} {v*100:.4f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
