"""研究步骤 34：高速抓取「不复权」日线到独立库 .data/raw_bars.db。

* 数据源：新浪 CN_MarketData.getKLineData（返回不复权原始价，1500 根覆盖 2020-08 至今）
* 写入独立的轻量 SQLite，先批量插入再建索引，避免污染主库
* 与行情软件盘面完全一致，可逐笔核对

用法::

    python research/02_data/34_fetch_raw_fast.py
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

ROOT = _p
DB = os.path.join(ROOT, ".data", "raw_bars.db")
META = os.path.join(ROOT, ".data", "meta.json")
SINA = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_local = threading.local()


def sess() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        _local.s = s
    return s


def sym(code: str) -> str:
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def fetch(code: str, datalen: int) -> list:
    for _ in range(3):
        try:
            r = sess().get(SINA, params={"symbol": sym(code), "scale": 240,
                                         "ma": "no", "datalen": datalen}, timeout=25)
            if r.status_code != 200:
                time.sleep(0.4)
                continue
            a = r.json()
            return a or []
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--datalen", type=int, default=1500)
    ap.add_argument("--batch", type=int, default=200)
    args = ap.parse_args()

    meta = json.load(open(META, encoding="utf-8")) if os.path.exists(META) else {}
    with sqlite3.connect(os.path.join(ROOT, ".data", "screener.db")) as c:
        codes = [r[0] for r in c.execute("SELECT DISTINCT code FROM daily_bars ORDER BY code")]
    codes = [x for x in codes if x.startswith(("000", "001", "002", "003",
                                               "600", "601", "603", "605"))]
    print(f"沪深主板 {len(codes)} 只，写入 {DB}")

    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE bars (code TEXT, date TEXT, open REAL, high REAL, "
                "low REAL, close REAL, volume REAL)")
    con.execute("CREATE TABLE names (code TEXT PRIMARY KEY, name TEXT)")

    t0 = time.time()
    done = rows = 0
    for i in range(0, len(codes), args.batch):
        batch = codes[i:i + args.batch]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            res = list(ex.map(lambda c: (c, fetch(c, args.datalen)), batch))
        payload = []
        for code, arr in res:
            for x in arr:
                try:
                    payload.append((code, x["day"], float(x["open"]), float(x["high"]),
                                    float(x["low"]), float(x["close"]),
                                    float(x.get("volume") or 0)))
                except Exception:  # noqa: BLE001
                    continue
        con.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", payload)
        con.executemany("INSERT OR REPLACE INTO names VALUES (?,?)",
                        [(c, (meta.get(c) or {}).get("name", "")) for c in batch])
        con.commit()
        done += len(batch)
        rows += len(payload)
        print(f"  {done}/{len(codes)}  {rows:,} 根  {done/max(time.time()-t0,1):.0f} 只/s")

    print("建索引 …")
    t = time.time()
    con.execute("CREATE INDEX ix ON bars(code, date)")
    con.execute("CREATE INDEX ixd ON bars(date)")
    con.commit()
    n = con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM bars").fetchone()
    con.close()
    print(f"完成：{n[0]:,} 根  {n[1]} ~ {n[2]}  建索引 {time.time()-t:.0f}s  "
          f"总用时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
