"""研究步骤 20b：用 requests + 线程池抓取 6 年日线（同花顺）与资金流向（东财）。

**为什么不用 httpx**：东方财富对 TLS 指纹敏感 —— httpx 会被重置连接
（``RemoteProtocolError``），而 ``requests`` / ``curl`` 正常。
同花顺同理。所以本脚本改用 ``requests`` + ``ThreadPoolExecutor``。

用法::

    python research/02_data/20_fetch_ths.py --only flow           # 资金流向（快，约 2 分钟）
    python research/02_data/20_fetch_ths.py --only ths --market sz  # 6 年日线（慢，建议限速）
    python research/02_data/20_fetch_ths.py --only ths --refresh     # 忽略已有，强制重抓
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

import requests  # noqa: E402

from stock_agent.screener.scanner import default_store_path  # noqa: E402
from stock_agent.screener.store import ScreenerStore  # noqa: E402

ROOT = _p
NAMES = os.path.join(ROOT, ".data", "names.json")
_THS_RE = re.compile(r"\((.*)\)\s*;?\s*$", re.S)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class RateLimiter:
    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / max(rate, 0.1)
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next_at:
                time.sleep(self.next_at - now)
                now = time.monotonic()
            self.next_at = now + self.interval


def to_secid(code: str) -> str:
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"1.{code}"
    return f"0.{code}"


def fetch_ths_year(sess: requests.Session, code: str, year: int, limiter: RateLimiter) -> List[Dict[str, Any]]:
    url = f"https://d.10jqka.com.cn/v6/line/hs_{code}/01/{year}.js"
    for attempt in range(3):
        try:
            limiter.wait()
            r = sess.get(url, timeout=15)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            m = _THS_RE.search(r.text.strip())
            if not m:
                return []
            data = (json.loads(m.group(1)).get("data") or "")
            out: List[Dict[str, Any]] = []
            for line in data.split(";"):
                p = line.split(",")
                if len(p) < 7 or len(p[0]) != 8:
                    continue
                d = f"{p[0][:4]}-{p[0][4:6]}-{p[0][6:8]}"
                try:
                    f: List[Any] = [float(x) if x not in ("", "-") else None for x in p]
                except ValueError:
                    continue
                out.append({
                    "date": d, "open": f[1], "high": f[2], "low": f[3], "close": f[4],
                    "volume": (f[5] / 100.0) if f[5] else None,
                    "amount": f[6],
                    "turnover_rate": f[7] if len(f) > 7 else None,
                })
            return out
        except Exception:  # noqa: BLE001
            time.sleep(0.35 * (attempt + 1))
    return []


def fetch_flow(sess: requests.Session, code: str, limiter: RateLimiter) -> List[Dict[str, Any]]:
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": to_secid(code),
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "klt": 101, "lmt": 0,
    }
    for attempt in range(3):
        try:
            limiter.wait()
            r = sess.get(url, params=params, timeout=15)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            klines = (r.json().get("data") or {}).get("klines") or []
            out: List[Dict[str, Any]] = []
            for line in klines:
                p = line.split(",")
                if len(p) < 13:
                    continue

                def gv(i: int) -> Optional[float]:
                    try:
                        return float(p[i]) if p[i] not in ("", "-") else None
                    except ValueError:
                        return None

                out.append({
                    "date": p[0], "main": gv(1), "small": gv(2), "medium": gv(3),
                    "large": gv(4), "xlarge": gv(5), "main_pct": gv(6),
                    "close": gv(11), "chg": gv(12),
                })
            return out
        except Exception:  # noqa: BLE001
            time.sleep(0.35 * (attempt + 1))
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2020,2021,2022,2023,2024,2025,2026")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--rate", type=float, default=12.0, help="全局每秒请求数上限")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", choices=["all", "ths", "flow"], default="all")
    ap.add_argument("--market", default="all",
                    choices=["all", "main", "sz", "sh", "zb", "sszb"])
    ap.add_argument("--min-bars", type=int, default=300, help="已有多少根就跳过（断点续传）")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    years = [int(x) for x in args.years.split(",") if x.strip()]

    store = ScreenerStore(default_store_path())

    def keep(c: str) -> bool:
        if c.startswith(("4", "8", "920")):
            return False
        if args.market == "main":
            return c.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))
        if args.market == "sz":
            return c.startswith(("000", "001", "002", "003", "300", "301"))
        if args.market == "sh":
            return c.startswith(("600", "601", "603", "605", "688", "689"))
        if args.market == "zb":
            return c.startswith(("000", "001", "002", "003"))
        if args.market == "sszb":
            return c.startswith(("600", "601", "603", "605"))
        return True

    codes = sorted(c for c in store.all_codes() if keep(c))
    if args.limit:
        codes = codes[: args.limit]

    skipped = 0
    if not args.refresh:
        with store._connect() as conn:
            conn.row_factory = None
            if args.only == "flow":
                have = {r[0] for r in conn.execute(
                    "SELECT code FROM money_flow GROUP BY code HAVING COUNT(*)>=500")}
            else:
                have = {r[0] for r in conn.execute(
                    "SELECT code FROM fetch_state WHERE adjust='bfq' AND bars>=?",
                    (args.min_bars,))}
        before = len(codes)
        codes = [c for c in codes if c not in have]
        skipped = before - len(codes)

    print(f"股票 {len(codes)} 只（跳过已完成 {skipped}）  workers={args.workers}  "
          f"限速={args.rate}/s  years={years if args.only != 'flow' else '-'}")

    limiter = RateLimiter(args.rate)
    # 重要：requests.Session 不是线程安全的 —— 每个线程一个
    _local = threading.local()

    def get_sess() -> requests.Session:
        s = getattr(_local, "sess", None)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": UA,
                "Referer": "https://data.eastmoney.com/",
                "Accept": "*/*",
            })
            _local.sess = s
        return s
    t0 = time.time()
    done = ok = fail = 0
    rows_n = 0
    lock = threading.Lock()

    def one(code: str) -> None:
        nonlocal done, ok, fail, rows_n
        sess = get_sess()
        if args.only == "flow":
            rows = fetch_flow(sess, code, limiter)
            if rows:
                store.put_money_flow(code, rows)
            n = len(rows)
        else:
            bars: List[Dict[str, Any]] = []
            for y in years:
                bars.extend(fetch_ths_year(sess, code, y, limiter))
            if bars:
                store.put_bars(code, "bfq", bars)
            n = len(bars)
        with lock:
            done += 1
            rows_n += n
            if n:
                ok += 1
            else:
                fail += 1
            if done % 200 == 0 or done == len(codes):
                el = time.time() - t0
                print(f"\r  {done}/{len(codes)} 成功{ok} 失败{fail} "
                      f"数据{rows_n:,} {done / el:.1f}只/s "
                      f"ETA {(len(codes) - done) / max(done / el, .01):.0f}s",
                      end="", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(one, c) for c in codes]
        for _ in as_completed(futs):
            pass
    print()
    print(f"完成 成功{ok} 失败{fail} 用时{time.time() - t0:.0f}s")
    print("缓存:", store.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
