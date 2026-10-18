"""研究步骤 22：抓取「大盘 + 板块」上下文数据（已适配限流）。

* 上证指数日线：腾讯 ``ifzq.gtimg.cn``（可拿 1500 根 ≈ 6 年）
* 股票 → 行业：东财 ``push2delay`` 的 ulist（批量 100 只/请求，未限流）
* 板块指数：**本地用成分股等权合成**（避免额外接口，且与个股口径一致）

输出 ``.data/market_ctx.json``::

    {"index": [[date,o,c,h,l,v,a], ...], "code_sector": {"002068": "橡胶", ...}}

用法::

    python research/02_data/22_fetch_context.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)

import requests  # noqa: E402

ROOT = _p
OUT = os.path.join(ROOT, ".data", "market_ctx.json")
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


def fetch_index(lim: int = 1500) -> List[List[Any]]:
    """上证指数（腾讯，最多 1500 根）。"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://gu.qq.com/"})
    for attempt in range(3):
        try:
            r = s.get(
                f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,{lim},",
                timeout=20,
            )
            node = r.json()["data"]["sh000001"]
            rows = node.get("day") or node.get("qfqday") or []
            out = []
            for x in rows:
                # [date, open, close, high, low, volume(手), ...]
                out.append([str(x[0]), x[1], x[2], x[3], x[4], x[5]])
            return out
        except Exception as exc:  # noqa: BLE001
            print(f"  指数重试 {attempt + 1}: {type(exc).__name__}")
            time.sleep(1.0 * (attempt + 1))
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=8.0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--bars", type=int, default=1500)
    args = ap.parse_args()
    limiter = RateLimiter(args.rate)

    out: Dict[str, Any] = {"index": [], "code_sector": {}}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            out["code_sector"] = old.get("code_sector") or {}
        except Exception:  # noqa: BLE001
            pass

    idx = fetch_index(args.bars)
    out["index"] = idx
    print(f"上证指数: {len(idx)} 根  {idx[0][0] if idx else '-'} ~ {idx[-1][0] if idx else '-'}")

    from stock_agent.screener.scanner import default_store_path  # noqa: E402
    from stock_agent.screener.store import ScreenerStore  # noqa: E402

    store = ScreenerStore(default_store_path())
    codes = [c for c in store.all_codes() if not c.startswith(("4", "8", "920"))]
    todo = [c for c in codes if c not in out["code_sector"]]
    print(f"股票 {len(codes)} 只，待映射 {len(todo)} 只")

    _local = threading.local()

    def sess() -> requests.Session:
        s = getattr(_local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
            _local.s = s
        return s

    batches = [todo[i : i + 100] for i in range(0, len(todo), 100)]
    done = 0
    lock = threading.Lock()

    def one(batch: List[str]) -> None:
        nonlocal done
        secids = ",".join(
            ("1." if c.startswith(("600", "601", "603", "605", "688", "689")) else "0.") + c
            for c in batch
        )
        for attempt in range(4):
            try:
                limiter.wait()
                r = sess().get(
                    "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
                    params={"fltt": 2, "invt": 2, "secids": secids, "fields": "f12,f100"},
                    timeout=20,
                )
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                for x in ((r.json().get("data") or {}).get("diff") or []):
                    if x.get("f12") and x.get("f100"):
                        out["code_sector"][x["f12"]] = x["f100"]
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.6 * (attempt + 1))
        with lock:
            done += 1
            if done % 10 == 0 or done == len(batches):
                print(f"\r  行业映射 {done}/{len(batches)} 批  已映射 {len(out['code_sector'])} 只",
                      end="", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(as_completed([ex.submit(one, b) for b in batches]))
    print()

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    secs = {}
    for v in out["code_sector"].values():
        secs[v] = secs.get(v, 0) + 1
    print(f"\n已保存 {OUT}")
    print(f"  指数 {len(out['index'])} 根 / 行业映射 {len(out['code_sector'])} 只 / {len(secs)} 个行业")
    for k, v in sorted(secs.items(), key=lambda x: -x[1])[:12]:
        print(f"    {k}: {v} 只")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
