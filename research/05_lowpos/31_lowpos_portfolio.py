"""研究步骤 31：低位票（低位均线粘合 / 突破）30 万组合回测。

数据
----
* 信号：research/05_lowpos/30_lowpos_breakout.py 生成的 .data/lowpos_signals.pkl
* 价格：.data/raw_bars_deep.db（新浪不复权，和行情软件盘面一致，2017-2026）

交易规则（全部可执行）
----------------------
* 2021-01-04 起步，30 万本金，最多同时持有 5 只
* 收盘出信号 → 次日开盘买入；一字涨停买不到则跳过
* 卖出：止损 / 止盈 / 移动止盈 / 到期 / 跌破 MA10，均次日开盘执行
* 买入 0.05%、卖出 0.10%（含印花税）
* 现金不够 / 停牌 / 一字跌停卖不出，都按真实情况处理

用法::

    python research/05_lowpos/31_lowpos_portfolio.py                     # 默认组合
    python research/05_lowpos/31_lowpos_portfolio.py --mode early        # 只做早期拐头
    python research/05_lowpos/31_lowpos_portfolio.py --mode break        # 只做突破
    python research/05_lowpos/31_lowpos_portfolio.py --grid              # 参数敏感性
    python research/05_lowpos/31_lowpos_portfolio.py --html              # 生成 HTML
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sqlite3
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

ROOT = _p
RAW = os.path.join(ROOT, ".data", "raw_bars_deep.db")
SIG = os.path.join(ROOT, ".data", "lowpos_signals.pkl")
EXTRA = os.path.join(ROOT, ".data", "signal_extra.pkl")
OUT = os.path.join(ROOT, ".data", "lowpos_sim.json")
HTML = os.path.join(ROOT, "低位票_30万回测.html")

FEE_BUY, FEE_SELL = 0.0005, 0.0010
START, END = "2021-01-04", "2026-10-16"


def clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def sig_score(s: Dict[str, Any], flavor: str = "combined") -> float:
    conv = clip((0.035 - s["spread20"]) / 0.035)
    pos = clip((0.70 - s["pos250"]) / 0.70)
    dd = clip(s["dd250"] / 0.60)
    mom = clip(s["chg1"] / 0.10, 0.0, 1.2)
    vol = clip(s["volR"] / 3.0)
    hs = clip(s["hist_slope3"] / 0.15)
    if flavor == "momentum":
        return 100 * (0.25 * conv + 0.45 * mom + 0.15 * vol + 0.05 * pos + 0.05 * dd + 0.05 * hs)
    if flavor == "quality":
        return 100 * (0.40 * conv + 0.30 * pos + 0.15 * dd + 0.05 * mom + 0.05 * vol + 0.05 * hs)
    if flavor == "mild":
        # 与 34_early_alpha.py 的「温和」完全一致：低位置 + 近20日弱 + 低价格
        return (-0.6 * s["pos250"] - 0.3 * s["ret20"] - 0.1 * s["close"] / 30)
    if flavor == "lowprice":
        return (-s["close"] / 30 - s["pos250"])
    if flavor == "lowvol":
        return -s["volR"]
    if flavor == "mild_mcap":
        return (-0.6 * s["pos250"] - 0.3 * s["ret20"] - 0.1 * s["close"] / 30
                - 0.15 * np.log1p(s.get("mcap", 1000.0)))
    if flavor == "mild_mcap_amt":
        return (-0.6 * s["pos250"] - 0.3 * s["ret20"] - 0.1 * s["close"] / 30
                - 0.15 * np.log1p(s.get("mcap", 1000.0))
                - 0.05 * np.log1p(s.get("amount20", 10.0)))
    base = 0.35 * conv + 0.20 * pos + 0.12 * dd + 0.10 * vol + 0.08 * hs
    if s["kind"] == "BREAK":
        base += 0.25 * mom + (0.03 if s["limit_up"] else 0.0)
    return 100 * base


class PriceBook:
    """按需读取某只股票的全部日线（带缓存）。"""

    def __init__(self, path: str):
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.con.row_factory = None
        self.cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        if code in self.cache:
            return self.cache[code]
        rows = self.con.execute(
            "SELECT date,open,high,low,close,volume FROM bars WHERE code=? ORDER BY date",
            (code,),
        ).fetchall()
        if not rows:
            self.cache[code] = None
            return None
        dates = [r[0] for r in rows]
        idx = {d: i for i, d in enumerate(dates)}
        o = np.array([r[1] or 0.0 for r in rows])
        h = np.array([r[2] or 0.0 for r in rows])
        l = np.array([r[3] or 0.0 for r in rows])
        c = np.array([r[4] or 0.0 for r in rows])
        vol = np.array([r[5] or 0.0 for r in rows])
        ma10 = np.full(len(c), np.nan)
        ma20 = np.full(len(c), np.nan)
        if len(c) >= 10:
            ma10[9:] = np.convolve(c, np.ones(10), "valid")
        if len(c) >= 20:
            ma20[19:] = np.convolve(c, np.ones(20), "valid")
        obj = {"dates": dates, "idx": idx, "open": o, "high": h, "low": l,
               "close": c, "vol": vol, "ma10": ma10, "ma20": ma20}
        self.cache[code] = obj
        return obj

    def bar(self, code: str, day: str) -> Optional[Tuple[int, float, float, float, float]]:
        p = self.get(code)
        if not p:
            return None
        i = p["idx"].get(day)
        if i is None:
            return None
        return i, p["open"][i], p["high"][i], p["low"][i], p["close"][i]


def trading_days(book: PriceBook, start: str, end: str) -> List[str]:
    rows = book.con.execute(
        "SELECT DISTINCT date FROM bars WHERE date>=? AND date<=? ORDER BY date",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def load_market_dates(path: str, rule: str) -> Optional[set]:
    if rule == "none":
        return None
    rows = json.load(open(path, encoding="utf-8"))
    for r in rows:
        if "date" not in r:
            r["date"] = r.get("day")
    closes = np.array([float(r["close"]) for r in rows], dtype=float)

    def ma(w: int) -> np.ndarray:
        out = np.full(len(closes), np.nan)
        if len(closes) >= w:
            out[w - 1:] = np.convolve(closes, np.ones(w), "valid") / w
        return out

    m20, m60 = ma(20), ma(60)
    ok = np.ones(len(rows), dtype=bool)
    if rule in ("ma20", "both") or rule.endswith("_ma20"):
        ok &= closes > m20
    if rule in ("ma60", "both") or rule.endswith("_ma60"):
        ok &= closes > m60
    if rule == "pullback":          # 上升趋势中的回调：指数 > MA60 但 <= MA20
        ok &= closes > m60
        ok &= closes <= m20
    if rule == "below_ma20":
        ok &= closes <= m20
    if rule == "below_ma60":
        ok &= closes <= m60
    return {r["date"] for r, o in zip(rows, ok) if o}


def select_by_day(signals: List[Dict[str, Any]], mode: str, flavor: str,
                  market_dates: Optional[set] = None,
                  extra: Optional[Dict[Any, Any]] = None,
                  max_mcap: float = 0.0, min_mcap: float = 0.0,
                  max_amount: float = 0.0, min_amount: float = 0.0,
                  max_price: float = 0.0
                  ) -> Dict[str, List[Dict[str, Any]]]:
    by: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in signals:
        if mode == "early" and s["kind"] != "EARLY":
            continue
        if mode == "break" and s["kind"] != "BREAK":
            continue
        name = (s.get("name") or "").upper().replace(" ", "")
        if "ST" in name or "退" in name:      # 排除 ST / 退市（回测里否则会出现 *ST、退市股）
            continue
        if s["close"] < 2.0:
            continue
        if max_price and s["close"] > max_price:
            continue
        if market_dates is not None and s["date"] not in market_dates:
            continue
        s = dict(s)
        if extra is not None:
            ex = extra.get((s["code"], s["date"]))
            if ex is None:
                continue
            mcap, amt = ex
            s["mcap"], s["amount20"] = mcap, amt
            if max_mcap and mcap > max_mcap:
                continue
            if min_mcap and mcap < min_mcap:
                continue
            if max_amount and amt > max_amount:
                continue
            if min_amount and amt < min_amount:
                continue
        if flavor == "early_first":
            s["score"] = sig_score(s, "mild" if s["kind"] == "EARLY" else "momentum")
        else:
            s["score"] = sig_score(s, flavor)
        by[s["date"]].append(s)
    for v in by.values():
        if flavor == "early_first":
            # 拐头优先，突破只在没满仓时补充（回测里突破没有超额）
            v.sort(key=lambda z: (0 if z["kind"] == "EARLY" else 1, -z["score"]))
        else:
            v.sort(key=lambda z: -z["score"])
    return by


def simulate(
    signals: List[Dict[str, Any]],
    book: PriceBook,
    *,
    mode: str = "combined",
    flavor: str = "combined",
    capital: float = 300_000,
    max_pos: int = 5,
    hold: int = 10,
    stop: float = 0.08,
    target: float = 0.20,
    trail: float = 0.0,
    trend_ma10: bool = False,
    start: str = START,
    end: str = END,
    max_price: float = 0.0,
    gap_max: float = 0.0,
    market_dates: Optional[set] = None,
    extra: Optional[Dict[Any, Any]] = None,
    max_mcap: float = 0.0, min_mcap: float = 0.0,
    max_amount: float = 0.0, min_amount: float = 0.0,
) -> Dict[str, Any]:
    by_day = select_by_day(signals, mode, flavor, market_dates, extra,
                           max_mcap, min_mcap, max_amount, min_amount, max_price)
    cal = trading_days(book, start, end)
    if not cal:
        return {"error": "no trading days"}

    cash = float(capital)
    pos: Dict[str, Dict[str, Any]] = {}
    sell_q: List[Tuple[str, str]] = []
    buy_q: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []

    def close_px(code: str, q: Dict[str, Any]) -> float:
        p = book.get(code)
        if p:
            return q["last"]
        return q["last"]

    for day in cal:
        # ---------- 1. 执行昨日收盘决定的卖出（次日开盘）----------
        remain: List[Tuple[str, str]] = []
        for code, reason in sell_q:
            q = pos.get(code)
            if not q:
                continue
            bar = book.bar(code, day)
            if bar is None:                       # 停牌，顺延
                remain.append((code, reason))
                continue
            i, op, hi, lo, cl = bar
            p = book.get(code)
            prev = p["close"][i - 1] if (p and i > 0) else None
            if prev and op <= prev * 0.905 and hi <= op * 1.001:   # 一字跌停卖不出
                remain.append((code, reason))
                continue
            sell_px = op * (1 - FEE_SELL)
            out = q["shares"] * sell_px
            cash += out
            pnl = out - q["cost"]
            trades.append({
                "code": code, "name": q["name"], "kind": q["kind"],
                "buy_date": q["buy_date"], "buy": round(q["buy"], 3),
                "shares": q["shares"], "amount_in": round(q["cost"], 2),
                "sell_date": day, "sell": round(op, 3), "amount_out": round(out, 2),
                "pnl": round(pnl, 2),
                "ret": round(pnl / q["cost"] * 100, 2) if q["cost"] else 0.0,
                "days": q["days"], "reason": reason,
            })
            pos.pop(code, None)
        sell_q = remain

        # ---------- 2. 执行昨日收盘决定的买入（次日开盘）----------
        for sig in buy_q:
            if len(pos) >= max_pos or cash <= 1000:
                break
            code = sig["code"]
            if code in pos:
                continue
            if max_price and sig["close"] > max_price:
                continue
            bar = book.bar(code, day)
            if bar is None:
                continue
            i, op, hi, lo, cl = bar
            if op <= 0:
                continue
            p = book.get(code)
            prev = p["close"][i - 1] if (p and i > 0) else None
            if prev and op >= prev * 1.095:       # 一字涨停买不到
                continue
            if gap_max and prev and op > prev * (1 + gap_max):   # 高开太多不追
                continue
            mv = 0.0
            for c2, q2 in pos.items():
                b2 = book.bar(c2, day)
                mv += q2["shares"] * (b2[1] if b2 else q2["last"])
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
                         "name": sig["name"], "kind": sig["kind"], "days": 0,
                         "last": op, "peak": op, "below_ma10": 0}
        buy_q = []

        # ---------- 3. 收盘估值 + 出场信号 ----------
        mv = 0.0
        for code, q in pos.items():
            bar = book.bar(code, day)
            if bar is not None:
                i, op, hi, lo, cl = bar
                q["last"] = cl
                q["peak"] = max(q["peak"], hi)
                q["days"] += 1
                p = book.get(code)
                q["below_ma10"] = (q["below_ma10"] + 1) if (p and cl < p["ma10"][i]) else 0
            mv += q["shares"] * q["last"]
        equity = cash + mv
        curve.append({"date": day, "equity": round(equity, 2),
                      "cash": round(cash, 2), "pos": len(pos)})

        for code, q in list(pos.items()):
            bar = book.bar(code, day)
            if bar is None:
                continue
            i, op, hi, lo, cl = bar
            reason = None
            if target and cl >= q["buy"] * (1 + target):
                reason = "止盈"
            elif stop and cl <= q["buy"] * (1 - stop):
                reason = "止损"
            elif trail and cl <= q["peak"] * (1 - trail):
                reason = "移动止盈"
            elif trend_ma10 and q["below_ma10"] >= 2:
                reason = "跌破MA10"
            elif q["days"] >= hold:
                reason = "到期"
            if reason and code not in {c for c, _ in sell_q}:
                sell_q.append((code, reason))

        # ---------- 4. 选明日买入（今天收盘的信号）----------
        avail = max_pos - len(pos) + len(sell_q)
        if avail > 0:
            held = set(pos) | {c for c, _ in sell_q}
            picked = []
            for s in by_day.get(day, []):
                if s["code"] in held:
                    continue
                picked.append(s)
                if len(picked) >= avail:
                    break
            buy_q = picked

    # ---------- 期末平仓 ----------
    last = cal[-1]
    if pos:
        for code, q in list(pos.items()):
            p = book.get(code)
            px = q["last"]
            if p and last in p["idx"]:
                px = p["close"][p["idx"][last]]
            out = q["shares"] * px * (1 - FEE_SELL)
            cash += out
            pnl = out - q["cost"]
            trades.append({
                "code": code, "name": q["name"], "kind": q["kind"],
                "buy_date": q["buy_date"], "buy": round(q["buy"], 3),
                "shares": q["shares"], "amount_in": round(q["cost"], 2),
                "sell_date": last, "sell": round(px, 3), "amount_out": round(out, 2),
                "pnl": round(pnl, 2),
                "ret": round(pnl / q["cost"] * 100, 2) if q["cost"] else 0.0,
                "days": q["days"], "reason": "期末平仓",
            })
            pos.pop(code)
        curve[-1]["equity"] = round(cash, 2)

    final = curve[-1]["equity"] if curve else capital
    peak = mdd = 0.0
    for p in curve:
        peak = max(peak, p["equity"])
        if peak:
            mdd = max(mdd, (peak - p["equity"]) / peak)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    years = max((np.datetime64(last) - np.datetime64(cal[0])).astype(int) / 365.25, 1e-9)
    avg_expo = float(np.mean([(p["equity"] - p["cash"]) / p["equity"] for p in curve])) if curve else 0.0
    return {
        "trades": trades, "curve": curve, "final": round(final, 2),
        "return_pct": round((final / capital - 1) * 100, 2),
        "cagr": round(((final / capital) ** (1 / years) - 1) * 100, 2),
        "mdd": round(mdd * 100, 2), "n_trades": len(trades),
        "win_rate": round(len(wins) / max(len(trades), 1) * 100, 1),
        "avg_win": round(np.mean([t["ret"] for t in wins]), 2) if wins else 0.0,
        "avg_loss": round(np.mean([t["ret"] for t in losses]), 2) if losses else 0.0,
        "profit_factor": round(gp / gl, 2) if gl else 0.0,
        "avg_days": round(np.mean([t["days"] for t in trades]), 1) if trades else 0.0,
        "avg_expo": round(avg_expo * 100, 1),
        "years": round(float(years), 2),
        "signals": sum(len(v) for v in by_day.values()),
        "mode": mode, "flavor": flavor, "hold": hold, "stop": stop,
        "target": target, "trail": trail, "trend_ma10": trend_ma10,
    }


VARIANTS = {
    "C1 组合10天-8%/20%": dict(mode="combined", flavor="combined", hold=10, stop=0.08, target=0.20, trail=0.0),
    "C2 组合15天-8%/20%": dict(mode="combined", flavor="combined", hold=15, stop=0.08, target=0.20, trail=0.0),
    "C3 组合20天-8%/20%": dict(mode="combined", flavor="combined", hold=20, stop=0.08, target=0.20, trail=0.0),
    "C4 组合10天-7%/15%": dict(mode="combined", flavor="combined", hold=10, stop=0.07, target=0.15, trail=0.0),
    "C5 组合10天-8%/无目标": dict(mode="combined", flavor="combined", hold=10, stop=0.08, target=0.0, trail=0.10),
    "C6 组合10天+MA10":   dict(mode="combined", flavor="combined", hold=15, stop=0.08, target=0.20, trail=0.0, trend_ma10=True),
    "E1 仅早期-10天":     dict(mode="early", flavor="quality", hold=10, stop=0.08, target=0.20, trail=0.0),
    "E2 仅早期-20天":     dict(mode="early", flavor="quality", hold=20, stop=0.08, target=0.20, trail=0.0),
    "B1 仅突破-5天":      dict(mode="break", flavor="momentum", hold=5, stop=0.06, target=0.12, trail=0.0),
    "B2 仅突破-10天":     dict(mode="break", flavor="momentum", hold=10, stop=0.08, target=0.15, trail=0.0),
    "M1 早期温和-20天-10%": dict(mode="early", flavor="mild", hold=20, stop=0.10, target=0.20, trail=0.0),
    "M2 早期温和-20天-无止损": dict(mode="early", flavor="mild", hold=20, stop=0.0, target=0.20, trail=0.0),
    "M3 早期温和-20天-12%": dict(mode="early", flavor="mild", hold=20, stop=0.12, target=0.20, trail=0.0),
    "M4 早期低价-20天-10%": dict(mode="early", flavor="lowprice", hold=20, stop=0.10, target=0.20, trail=0.0),
    "M5 早期低量-20天-10%": dict(mode="early", flavor="lowvol", hold=20, stop=0.10, target=0.20, trail=0.0),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="combined", choices=["combined", "early", "break"])
    ap.add_argument("--flavor", default="combined", choices=["combined", "momentum", "quality", "mild", "lowprice", "lowvol", "early_first", "mild_mcap", "mild_mcap_amt"])
    ap.add_argument("--capital", type=float, default=300_000)
    ap.add_argument("--max-pos", type=int, default=5)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--target", type=float, default=0.20)
    ap.add_argument("--trail", type=float, default=0.0)
    ap.add_argument("--trend-ma10", action="store_true")
    ap.add_argument("--max-price", type=float, default=0.0)
    ap.add_argument("--max-mcap", type=float, default=0.0, help="流通市值上限（亿）")
    ap.add_argument("--min-mcap", type=float, default=0.0, help="流通市值下限（亿）")
    ap.add_argument("--max-amount", type=float, default=0.0, help="20日均成交额上限（亿）")
    ap.add_argument("--min-amount", type=float, default=0.0, help="20日均成交额下限（亿）")
    ap.add_argument("--gap-max", type=float, default=0.0,
                    help="次日开盘较信号日收盘高开超过该比例则不买（默认 0=不限）")
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--grid", action="store_true", help="跑一组参数对比")
    ap.add_argument("--market-filter", default="none",
                    choices=["none", "ma20", "ma60", "both", "sh_ma20", "sh_ma60",
                              "zz500_ma20", "zz500_ma60",
                              "zz1000_ma20", "zz1000_ma60",
                              "gz2000_ma20", "gz2000_ma60",
                              "pullback", "below_ma20", "below_ma60"],
                    help="大盘过滤：指数收盘在 MA20/MA60 上方才买")
    ap.add_argument("--html", action="store_true", help="生成 HTML 报告")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    signals = pickle.load(open(SIG, "rb"))
    book = PriceBook(RAW)
    extra = pickle.load(open(EXTRA, "rb")) if os.path.exists(EXTRA) else None
    idx_file = {
        "sh_ma20": "index_sh_full.json", "sh_ma60": "index_sh_full.json",
        "zz500_ma20": "index_sh000905.json", "zz500_ma60": "index_sh000905.json",
        "zz1000_ma20": "index_sh000852.json", "zz1000_ma60": "index_sh000852.json",
        "gz2000_ma20": "index_sz399303.json", "gz2000_ma60": "index_sz399303.json",
    }.get(args.market_filter, "index_sh_full.json")
    market_dates = load_market_dates(os.path.join(ROOT, ".data", idx_file),
                                     args.market_filter)

    if args.grid:
        print("=" * 112)
        print(f"{'变体':<24}{'交易':>6}{'期末权益':>13}{'收益%':>10}{'年化%':>9}"
              f"{'回撤%':>8}{'胜率%':>7}{'PF':>7}{'均持':>6}{'仓位%':>7}")
        print("=" * 112)
        results = {}
        for name, v in VARIANTS.items():
            kw = dict(v)
            kw.setdefault("max_price", args.max_price)
            r = simulate(signals, book, capital=args.capital, max_pos=args.max_pos,
                         start=args.start, end=args.end, market_dates=market_dates,
                         extra=extra, max_mcap=args.max_mcap, min_mcap=args.min_mcap,
                         max_amount=args.max_amount, min_amount=args.min_amount,
                         max_price=args.max_price, gap_max=args.gap_max, **kw)
            results[name] = r
            print(f"{name:<24}{r['n_trades']:>6}{r['final']:>13,.0f}{r['return_pct']:>10.1f}"
                  f"{r['cagr']:>9.1f}{r['mdd']:>8.1f}{r['win_rate']:>7.1f}"
                  f"{r['profit_factor']:>7.2f}{r['avg_days']:>6.1f}{r['avg_expo']:>7.1f}")
        print("=" * 112)
        best = max(results, key=lambda k: results[k]["return_pct"])
        print(f"收益最高：{best}  {results[best]['return_pct']:+.2f}%")
        best_q = max(results, key=lambda k: (results[k]["return_pct"] /
                                             max(results[k]["mdd"], 1)))
        print(f"收益/回撤最优：{best_q}  {results[best_q]['return_pct']:+.2f}%  "
              f"回撤 {results[best_q]['mdd']:.1f}%")
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False)
        print(f"明细 → {args.out}")
        return 0

    r = simulate(signals, book, mode=args.mode, flavor=args.flavor,
                 capital=args.capital, max_pos=args.max_pos,
                 hold=args.hold, stop=args.stop, target=args.target,
                 trail=args.trail, trend_ma10=args.trend_ma10,
                 start=args.start, end=args.end, max_price=args.max_price,
                 gap_max=args.gap_max, market_dates=market_dates, extra=extra,
                 max_mcap=args.max_mcap, min_mcap=args.min_mcap,
                 max_amount=args.max_amount, min_amount=args.min_amount)
    print(f"{args.mode}/{args.flavor}  hold={args.hold} stop={args.stop} "
          f"target={args.target} trail={args.trail} ma10={args.trend_ma10}")
    print(f"期末 {r['final']:,.0f}  收益 {r['return_pct']:+.2f}%  年化 {r['cagr']:+.2f}%  "
          f"回撤 {r['mdd']:.2f}%  交易 {r['n_trades']}  胜率 {r['win_rate']:.1f}%  "
          f"PF {r['profit_factor']:.2f}  均持 {r['avg_days']:.1f}天  仓位 {r['avg_expo']:.1f}%")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(r, fh, ensure_ascii=False)
    print(f"明细 → {args.out}")
    if args.html:
        build_html(r, args.out.replace(".json", ".html"))
    return 0


# ---------------------------------------------------------------- HTML 报告

HTML_HEAD = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>低位票 · 30万实盘模拟回测报告</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e15;color:#e7ecf5;font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;padding:26px 30px 60px}
h1{font-size:22px;margin-bottom:4px}
.sub{color:#7d8799;font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
.card{background:#141924;border:1px solid #232b3a;border-radius:12px;padding:14px 16px}
.card span{display:block;font-size:11.5px;color:#7d8799;margin-bottom:6px}
.card i{font-style:normal;font-size:21px;font-weight:800}
.up{color:#ff4d3d}.dn{color:#00c076}.acc{color:#ffb347}.blue{color:#4a9eff}
#chart{height:400px;background:#141924;border:1px solid #232b3a;border-radius:12px;margin-bottom:22px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{position:sticky;top:0;background:#1a202c;color:#9aa5b8;font-weight:600;text-align:left;padding:9px 10px;border-bottom:1px solid #232b3a;white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid #1a202c;white-space:nowrap}
tbody tr:hover{background:#171d29}
.wrap{max-height:680px;overflow:auto;border:1px solid #232b3a;border-radius:12px}
.note{background:linear-gradient(90deg,rgba(255,138,61,.1),transparent);border:1px solid rgba(255,138,61,.28);border-radius:10px;padding:12px 16px;font-size:12.5px;color:#9aa5b8;margin-bottom:20px}
.note b{color:#ffb347}
.tag{padding:1px 7px;border-radius:5px;font-size:11px}
.t-win{background:rgba(255,77,61,.15);color:#ff4d3d}
.t-loss{background:rgba(0,192,118,.15);color:#00c076}
h2{font-size:15px;margin:22px 0 10px;display:flex;align-items:center;gap:8px}
h2::before{content:"";width:3px;height:14px;background:#ff8a3d;border-radius:2px}
</style></head><body>"""


def build_html(r: Dict[str, Any], out: str) -> None:
    from collections import defaultdict
    trades = r["trades"]
    curve = r["curve"]
    year_pnl: Dict[str, float] = defaultdict(float)
    year_n: Dict[str, int] = defaultdict(int)
    for t in trades:
        y = t["sell_date"][:4]
        year_pnl[y] += t["pnl"]
        year_n[y] += 1
    eq_year: Dict[str, Any] = {}
    for p in curve:
        eq_year.setdefault(p["date"][:4], []).append(p["equity"])
    year_ret: Dict[str, float] = {}
    prev_eq = 300000.0
    for y in sorted(eq_year):
        end_eq = eq_year[y][-1]
        year_ret[y] = (end_eq / prev_eq - 1) * 100 if prev_eq else 0.0
        prev_eq = end_eq
    year_rows = "".join(
        f"<tr><td>{y}</td><td>{year_n[y]}</td>"
        f"<td class='{'up' if year_pnl[y] > 0 else 'dn'}'>{year_pnl[y]:+,.0f}</td>"
        f"<td class='{'up' if year_ret[y] > 0 else 'dn'}'>{year_ret[y]:+.1f}%</td></tr>"
        for y in sorted(year_n))
    rows = []
    for i, t in enumerate(trades, 1):
        cl = "up" if t["pnl"] > 0 else "dn"
        tag = "t-win" if t["pnl"] > 0 else "t-loss"
        rows.append(
            f"<tr><td>{i}</td><td>{t['code']}</td><td>{t['name']}</td>"
            f"<td>{t['buy_date']}</td><td>{t['buy']:.2f}</td><td>{t['shares']:,}</td>"
            f"<td>{t['amount_in']:,.0f}</td><td>{t['sell_date']}</td><td>{t['sell']:.2f}</td>"
            f"<td>{t['amount_out']:,.0f}</td>"
            f"<td class='{cl}'>{t['pnl']:+,.0f}</td>"
            f"<td><span class='tag {tag}'>{t['ret']:+.2f}%</span></td>"
            f"<td>{t['days']}</td><td>{t['reason']}</td>"
            f"<td>{'突破' if t['kind'] == 'BREAK' else '拐头'}</td></tr>")
    ret_cls = "up" if r["return_pct"] > 0 else "dn"
    mode_txt = {"combined": "组合（早期拐头 + 突破）",
                "early": "早期拐头（买在启动前）", "break": "突破加速（观察模式）"}[r["mode"]]
    html = HTML_HEAD + f"""
<h1>低位票 · 30 万实盘模拟回测</h1>
<div class="sub" style="color:#ffb347;font-weight:700">{mode_txt} ｜ 最多持有 4 只 ｜ 中证1000 > MA20 才买 ｜ 2021-01-04 ~ {curve[-1]['date']}</div>
<div class="sub">次日开盘买入 ｜ 卖出次日开盘执行 ｜ 不复权真实价格 ｜ 买入 0.05% / 卖出 0.10% 已计 ｜ 流动性下限 0.2 亿/日</div>
<div class="note">
<b>策略规则：</b>① 每天收盘扫描「低位 + 均线粘合 + 刚拐头」的票；
② 排名 = 低 250 日位置 + 近 20 日偏弱 + 低价格 + <b>小市值</b> + <b>低成交额</b>；
③ 次日开盘买入，最多 4 只、每只约 1/4 权益；④ 出场：<b>+25% 止盈 / -15% 灾难止损 / 20 个交易日到期</b>，均次日开盘执行；
⑤ 排除 ST / 退市 / 股价 &lt; 2 元、20 日均成交额 &lt; 0.2 亿；一字涨停买不到、一字跌停卖不出、停牌均按真实情况处理。
</div>
<div class="note"><b>诚实说明：</b>这是 2021-2026 的<b>样本内</b>回测，不是收益承诺。收益的最大来源是<b>小市值 + 低流动性</b>因子
（2021-2023 小市值行情尤其强），这类股票容量有限、滑点和冲击成本更高；回测未额外计滑点，实盘会更差。
排名权重、止损/止盈和持有期都在同一段数据上选过，未来可能失效；2023 年几乎不赚钱、个别月份会连续回撤。
价格为<b>不复权</b>成交价，分红送转未计入；ST/退市按当前名称排除（轻微事后偏差）。</div>
<div class="grid">
  <div class="card"><span>初始本金</span><i>300,000</i></div>
  <div class="card"><span>期末权益</span><i class="acc">{r['final']:,.0f}</i></div>
  <div class="card"><span>总收益</span><i class="{ret_cls}">{r['return_pct']:+.2f}%</i></div>
  <div class="card"><span>年化收益</span><i class="{ret_cls}">{r['cagr']:+.2f}%</i></div>
  <div class="card"><span>最大回撤</span><i class="dn">{r['mdd']:.2f}%</i></div>
  <div class="card"><span>交易笔数</span><i>{r['n_trades']}</i></div>
  <div class="card"><span>胜率</span><i class="blue">{r['win_rate']:.1f}%</i></div>
  <div class="card"><span>平均盈利</span><i class="up">{r['avg_win']:+.2f}%</i></div>
  <div class="card"><span>平均亏损</span><i class="dn">{r['avg_loss']:+.2f}%</i></div>
  <div class="card"><span>盈亏比(PF)</span><i>{r['profit_factor']:.2f}</i></div>
  <div class="card"><span>平均持有</span><i>{r['avg_days']:.1f} 天</i></div>
  <div class="card"><span>平均仓位</span><i>{r['avg_expo']:.1f}%</i></div>
</div>
<div id="chart"></div>
<h2>年度表现</h2>
<div class="wrap" style="max-height:260px"><table><thead><tr><th>年份</th><th>交易笔数</th><th>盈亏（元，按卖出年）</th><th>年末权益收益率</th></tr></thead>
<tbody>{year_rows}</tbody></table></div>
<h2>全部交易（{len(trades)} 笔）</h2>
<div class="wrap"><table><thead><tr>
<th>#</th><th>代码</th><th>名称</th><th>买入日</th><th>买入价</th><th>股数</th><th>买入金额</th>
<th>卖出日</th><th>卖出价</th><th>卖出金额</th><th>盈亏</th><th>收益率</th><th>持有天数</th><th>卖出原因</th><th>信号</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<script>
const data = {json.dumps([{"date": p["date"], "equity": p["equity"]} for p in curve], ensure_ascii=False)};
const chart = echarts.init(document.getElementById('chart'));
chart.setOption({{
  backgroundColor:'transparent',
  grid:{{left:70,right:24,top:30,bottom:50}},
  tooltip:{{trigger:'axis'}},
  xAxis:{{type:'category',data:data.map(x=>x.date),axisLine:{{lineStyle:{{color:'#2a3446'}}}},axisLabel:{{color:'#7d8799'}}}},
  yAxis:{{type:'value',scale:true,axisLabel:{{color:'#7d8799'}},splitLine:{{lineStyle:{{color:'#1c2432'}}}}}},
  series:[{{name:'权益',type:'line',showSymbol:false,smooth:true,lineStyle:{{width:2,color:'#ff8a3d'}},
    areaStyle:{{color:'rgba(255,138,61,.15)'}},data:data.map(x=>x.equity)}}]
}});
window.addEventListener('resize',()=>chart.resize());
</script>
</body></html>"""
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"HTML → {out}")


if __name__ == "__main__":
    raise SystemExit(main())
