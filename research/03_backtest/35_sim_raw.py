"""研究步骤 35：用「不复权真实价格」重做 30 万组合回测。

修正三件事：
  1. 数据源换成 raw_bars.db（新浪不复权，与行情软件盘面一致，可逐笔核对）
  2. 排除 ST（之前回测里出现 *ST 是 bug）
  3. 按用户提供的形态样本收紧：涨停后横盘 3~8 天、平台窄且不下移、日K小波动、量能不放不缩

规则：
  * 收盘选股 → 次日开盘买入（一字涨停买不到则跳过）
  * 卖出：涨停止盈 / 跌破横盘下沿 / 超时，均次日开盘执行
  * 费用：买 0.05%，卖 0.10%（含印花税）
  * 最多同时持有 5 只，单票 = 总权益/5
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from array import array
from collections import defaultdict
from typing import Any, Dict, List

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

from stock_agent.screener.patterns import (  # noqa: E402
    FEATURE_KEYS,
    MineConfig,
    SecondBoardConfig,
    compute_score,
    mine_stock,
    structure_reasons,
)

ROOT = _p
RAW = os.path.join(ROOT, ".data", "raw_bars.db")
META = os.path.join(ROOT, ".data", "meta.json")
CACHE = os.path.join(ROOT, ".data", "sim_raw_classic.pkl")
OUT = os.path.join(ROOT, ".data", "sim_raw_classic.json")

FEE_BUY, FEE_SELL = 0.0005, 0.0010
START, END = "2021-01-01", "2026-10-17"


def is_st(name: str) -> bool:
    n = (name or "").upper().replace(" ", "")
    return "ST" in n or "退" in n


def load_raw() -> Dict[str, Any]:
    """读取 raw_bars.db，按股票分组。"""
    import sqlite3
    con = sqlite3.connect(f"file:{RAW}?mode=ro", uri=True)
    con.row_factory = None
    names = dict(con.execute("SELECT code, name FROM names").fetchall())
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for code, date, o, h, lo, c, v in con.execute(
            "SELECT code, date, open, high, low, close, volume FROM bars "
            "WHERE date>='2020-06-01' ORDER BY code, date"):
        out[code].append({"date": date, "open": o, "high": h, "low": lo,
                          "close": c, "volume": v})
    con.close()
    return {"names": names, "bars": dict(out)}


def mine(cfg: SecondBoardConfig, force: bool = False) -> Dict[str, Any]:
    if os.path.exists(CACHE) and not force:
        with open(CACHE, "rb") as fh:
            return pickle.load(fh)
    data = load_raw()
    bars_all, names = data["bars"], data["names"]
    print(f"股票 {len(bars_all)} 只，开始挖掘（真实不复权价格）…")
    mcfg = MineConfig(anchor_min_gain=cfg.anchor_min_gain,
                      anchor_max_gain=cfg.anchor_max_gain,
                      min_gap=2, max_gap=15, max_abs_dev=0.12)
    base = SecondBoardConfig()
    base.min_gap, base.max_gap = 2, 15
    base.close_band, base.max_range_pct = 0.10, 0.20
    base.min_vol_trend, base.max_avg_amp = 0.0, 9.99
    base.min_platform_drift = -9.99

    samples: List[Dict[str, Any]] = []
    prices: Dict[str, Any] = {}
    n = 0
    for code, bars in bars_all.items():
        name = names.get(code, "")
        if is_st(name) or len(bars) < 150:
            continue
        n += 1
        prices[code] = {
            "date": [b["date"] for b in bars],
            "open": array("d", [b["open"] or 0.0 for b in bars]),
            "close": array("d", [b["close"] or 0.0 for b in bars]),
        }
        for r in mine_stock(bars, code, name, mcfg):
            d = r.get("decision_date") or ""
            if d < START or d > END:
                continue
            feats = {k: r.get(k) for k in FEATURE_KEYS if k in r}
            if structure_reasons(feats, base):
                continue
            samples.append({
                "d": d, "code": code, "name": name,
                "score": float(compute_score(feats)) * 100,
                "anchor": float(r.get("limit_price") or 0),
                "gap": int(feats.get("gap") or 0),
                "pd": float(feats.get("platform_drift") or 0.0),
                "amp": float(feats.get("avg_amp") or 0.0),
                "vt": float(feats.get("vol_trend") or 0.0),
                "rng": float(feats.get("range_pct") or 0.0),
                "dcmax": float(feats.get("dev_close_max") or 0.0),
                "dcmin": float(feats.get("dev_close_min") or 0.0),
            })
        if n % 500 == 0:
            print(f"  {n} 只 … 样本 {len(samples):,}")
    print(f"挖掘完成：样本 {len(samples):,} 条")
    obj = {"samples": samples, "prices": prices}
    with open(CACHE, "wb") as fh:
        pickle.dump(obj, fh)
    return obj


def simulate(samples, prices, v: Dict[str, float], capital=300_000, max_pos=5) -> Dict[str, Any]:
    gmin, gmax = int(v.get("gap_min", 3)), int(v.get("gap_max", 8))
    band = float(v.get("band", 0.05))
    amp = float(v.get("amp", 0.05))
    pdm = float(v.get("pd", -0.01))
    vtm = float(v.get("vt", 0.8))
    hold = int(v.get("hold", 10))
    stop_band = float(v.get("stop", 0.07))
    desc = bool(v.get("desc", True))

    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in samples:
        if not (gmin <= s["gap"] <= gmax):
            continue
        if s["pd"] < pdm or s["amp"] > amp or s["vt"] < vtm:
            continue
        if s["dcmax"] > band or s["dcmin"] < -band:
            continue
        by_day[s["d"]].append(s)
    for x in by_day.values():
        x.sort(key=lambda z: -z["score"] if desc else z["score"])

    cal = sorted({d for p in prices.values() for d in p["date"] if d >= START})
    idx = {c: {d: i for i, d in enumerate(p["date"])} for c, p in prices.items()}

    def px(code, day, f="open"):
        p = prices.get(code)
        if not p:
            return None
        i = idx[code].get(day)
        if i is None:
            return None
        return p[f][i] or None

    cash, pos = capital, {}
    sell_q: List[Any] = []
    buy_q: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []

    for day in cal:
        for code, reason in sell_q:
            q = pos.pop(code, None)
            if not q:
                continue
            p = px(code, day) or px(code, day, "close") or q["last"]
            out = q["shares"] * p * (1 - FEE_SELL)
            cash += out
            trades.append({"code": code, "name": q["name"], "buy_date": q["buy_date"],
                           "buy": round(q["buy"], 3), "shares": q["shares"],
                           "sell_date": day, "sell": round(p, 3),
                           "amount_in": round(q["cost"], 2), "amount_out": round(out, 2),
                           "pnl": round(out - q["cost"], 2),
                           "ret": round((out - q["cost"]) / q["cost"] * 100, 2),
                           "days": q["days"], "reason": reason})
        sell_q = []

        for sig in buy_q:
            if len(pos) >= max_pos:
                break
            code = sig["code"]
            if code in pos:
                continue
            op = px(code, day)
            if not op:
                continue
            i = idx[code].get(day, 0)
            prev = prices[code]["close"][i - 1] if i > 0 else None
            if prev and op >= prev * 1.095:      # 一字涨停买不到
                continue
            mv = sum(q2["shares"] * (px(c, day) or q2["last"]) for c, q2 in pos.items())
            budget = min(cash, (cash + mv) / max_pos)
            sh = int(budget / (op * 100)) * 100
            if sh <= 0:
                continue
            cost = sh * op * (1 + FEE_BUY)
            if cost > cash:
                sh = int(cash / (op * 100 * (1 + FEE_BUY))) * 100
                if sh <= 0:
                    continue
                cost = sh * op * (1 + FEE_BUY)
            cash -= cost
            pos[code] = {"shares": sh, "buy": op, "cost": cost, "buy_date": day,
                         "name": sig["name"], "stop": sig["anchor"] * (1 - stop_band),
                         "days": 0, "last": op}
        buy_q = []

        mv = 0.0
        for code, q in pos.items():
            c = px(code, day, "close") or q["last"]
            q["last"] = c
            mv += q["shares"] * c
        curve.append({"date": day, "equity": round(cash + mv, 2)})

        for code, q in list(pos.items()):
            cl = px(code, day, "close")
            if not cl:
                continue
            q["days"] += 1
            i = idx[code].get(day, 0)
            prev = prices[code]["close"][i - 1] if i > 0 else None
            chg = (cl / prev - 1) * 100 if prev else 0.0
            if chg >= 9.5:
                sell_q.append((code, "二次涨停止盈"))
            elif cl < q["stop"]:
                sell_q.append((code, "跌破止损"))
            elif q["days"] >= hold:
                sell_q.append((code, "到期离场"))

        if len(pos) < max_pos:
            held = set(pos)
            buy_q = [s for s in by_day.get(day, []) if s["code"] not in held][: max_pos - len(pos)]

    if pos:
        last = cal[-1]
        for code, q in list(pos.items()):
            p = px(code, last, "close") or q["last"]
            out = q["shares"] * p * (1 - FEE_SELL)
            cash += out
            trades.append({"code": code, "name": q["name"], "buy_date": q["buy_date"],
                           "buy": round(q["buy"], 3), "shares": q["shares"],
                           "sell_date": last, "sell": round(p, 3),
                           "amount_in": round(q["cost"], 2), "amount_out": round(out, 2),
                           "pnl": round(out - q["cost"], 2),
                           "ret": round((out - q["cost"]) / q["cost"] * 100, 2),
                           "days": q["days"], "reason": "期末平仓"})
            pos.pop(code)
        curve[-1]["equity"] = round(cash, 2)

    final = curve[-1]["equity"] if curve else capital
    peak = mdd = 0.0
    for p in curve:
        peak = max(peak, p["equity"])
        if peak:
            mdd = max(mdd, (peak - p["equity"]) / peak)
    wins = [t for t in trades if t["pnl"] > 0]
    loss = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in loss)
    return {"trades": trades, "curve": curve, "final": round(final, 2),
            "return_pct": round((final / capital - 1) * 100, 2), "mdd": round(mdd * 100, 2),
            "n_trades": len(trades),
            "win_rate": round(len(wins) / max(len(trades), 1) * 100, 1),
            "avg_win": round(sum(t["ret"] for t in wins) / max(len(wins), 1), 2),
            "avg_loss": round(sum(t["ret"] for t in loss) / max(len(loss), 1), 2),
            "profit_factor": round(gp / gl, 2) if gl else 0.0,
            "avg_days": round(sum(t["days"] for t in trades) / max(len(trades), 1), 1),
            "signals": sum(len(x) for x in by_day.values())}


VARIANTS = {
    "S1 4-8天+量能≥0.9":         dict(gap_min=4, gap_max=8, band=0.04, amp=0.04, pd=-0.01, vt=0.9, hold=10, stop=0.07),
    "S2 4-8天+量能≥0.9+止损9%":  dict(gap_min=4, gap_max=8, band=0.04, amp=0.04, pd=-0.01, vt=0.9, hold=10, stop=0.09),
    "S3 4-8天+量能≥0.9+持15天":  dict(gap_min=4, gap_max=8, band=0.04, amp=0.04, pd=-0.01, vt=0.9, hold=15, stop=0.07),
    "S4 3-8天+量能≥0.9":         dict(gap_min=3, gap_max=8, band=0.04, amp=0.04, pd=-0.01, vt=0.9, hold=10, stop=0.07),
    "S5 4-9天+量能≥0.9":         dict(gap_min=4, gap_max=9, band=0.04, amp=0.04, pd=-0.01, vt=0.9, hold=10, stop=0.07),
    "S6 4-8天+量能≥1.0":         dict(gap_min=4, gap_max=8, band=0.04, amp=0.04, pd=-0.01, vt=1.0, hold=10, stop=0.07),
    "S7 4-8天+量能≥0.9+窄3.5%":  dict(gap_min=4, gap_max=8, band=0.035, amp=0.04, pd=-0.01, vt=0.9, hold=10, stop=0.07),
    "S8 4-8天+量能≥0.9+平台上移": dict(gap_min=4, gap_max=8, band=0.04, amp=0.04, pd=0.0, vt=0.9, hold=10, stop=0.07),
    "S9 4-8天+量能≥0.9+持8天":   dict(gap_min=4, gap_max=8, band=0.04, amp=0.04, pd=-0.01, vt=0.9, hold=8, stop=0.07),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--anchor-min", type=float, default=9.4)
    ap.add_argument("--anchor-max", type=float, default=10.5)
    ap.add_argument("--tag", default="classic")
    args = ap.parse_args()
    global CACHE, OUT
    CACHE = os.path.join(ROOT, ".data", f"sim_raw_{args.tag}.pkl")
    OUT = os.path.join(ROOT, ".data", f"sim_raw_{args.tag}.json")
    cfg = SecondBoardConfig()
    cfg.anchor_min_gain = args.anchor_min
    cfg.anchor_max_gain = args.anchor_max
    obj = mine(cfg, force=args.force)
    samples, prices = obj["samples"], obj["prices"]

    print("\n" + "=" * 98)
    print(f"{'变体':<28}{'信号':>8}{'交易':>7}{'期末权益':>13}{'收益%':>10}{'回撤%':>9}{'胜率%':>8}{'PF':>7}{'均持':>7}")
    print("=" * 98)
    res = {}
    for k, v in VARIANTS.items():
        r = simulate(samples, prices, v)
        res[k] = r
        print(f"{k:<28}{r['signals']:>8,}{r['n_trades']:>7}{r['final']:>13,.0f}"
              f"{r['return_pct']:>10.1f}{r['mdd']:>9.1f}{r['win_rate']:>8.1f}"
              f"{r['profit_factor']:>7.2f}{r['avg_days']:>7.1f}")
    print("=" * 98)
    best = max(res, key=lambda k: res[k]["return_pct"])
    print(f"最优：{best}  {res[best]['return_pct']:+.2f}%  期末 {res[best]['final']:,.0f}")
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False)
    print(f"明细 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
