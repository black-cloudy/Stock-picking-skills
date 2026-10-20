"""选股结果本地缓存（SQLite）。

设计目标
--------
1. **日线持久化**：全市场日线只需拉一次，之后所有策略扫描都是本地查询（毫秒级）。
2. **新鲜度控制**：交易日盘中 5 分钟内视为新鲜，收盘后 / 非交易日长期有效。
3. **断点续传**：只补拉缺失或过期的标的，不重复请求上游。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..utils import market_phase

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars (
    code       TEXT NOT NULL,
    adjust     TEXT NOT NULL,
    date       TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     REAL,
    amount     REAL,
    change_pct REAL,
    turnover_rate REAL,
    PRIMARY KEY (code, adjust, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_code ON daily_bars(code, adjust);
CREATE INDEX IF NOT EXISTS idx_daily_adj_code_date ON daily_bars(adjust, code, date);
CREATE INDEX IF NOT EXISTS idx_daily_adj_date ON daily_bars(adjust, date, change_pct);

CREATE TABLE IF NOT EXISTS fetch_state (
    code       TEXT NOT NULL,
    adjust     TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    bars       INTEGER NOT NULL,
    PRIMARY KEY (code, adjust)
);

CREATE TABLE IF NOT EXISTS money_flow (
    code   TEXT NOT NULL,
    date   TEXT NOT NULL,
    main   REAL,      -- 主力净流入(元)
    small  REAL,      -- 小单净流入
    medium REAL,      -- 中单净流入
    large  REAL,      -- 大单净流入
    xlarge REAL,      -- 超大单净流入
    main_pct REAL,    -- 主力净占比 %
    close  REAL,
    chg    REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_flow_code_date ON money_flow(code, date);

CREATE TABLE IF NOT EXISTS kv (
    k  TEXT PRIMARY KEY,
    v  TEXT NOT NULL,
    ts REAL NOT NULL
);
"""


def default_ttl(now: Optional[float] = None) -> float:
    """当前时点日线缓存的有效期（秒）。"""
    phase = market_phase()
    if phase in ("trading", "call_auction"):
        return 300.0          # 盘中 5 分钟
    if phase == "lunch_break":
        return 900.0
    return 12 * 3600.0        # 盘后 / 周末


class ScreenerStore:
    """SQLite 缓存。线程安全（每次操作使用独立连接）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    # ------------------------------------------------------------ 连接

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(SCHEMA)
            # 兼容旧库：补列
            cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_bars)")}
            if "turnover_rate" not in cols:
                conn.execute("ALTER TABLE daily_bars ADD COLUMN turnover_rate REAL")

    # ------------------------------------------------------------ 日线

    def get_bars(self, code: str, adjust: str = "none") -> List[Dict[str, Any]]:
        return self.get_many([code], adjust).get(code, [])

    def get_many(
        self,
        codes: Sequence[str],
        adjust: str = "none",
        tail: Optional[int] = None,
        since: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """批量读取日线（一次查询拿完，供全市场扫描用）。

        ``tail``  只取每只股票最近 N 根（需窗口函数，较慢）；
        ``since`` 只取某日期之后的全部（走日期索引，快很多）。两者同给时优先后者。
        """
        out: Dict[str, List[Dict[str, Any]]] = {c: [] for c in codes}
        chunk = list(dict.fromkeys(codes))
        with self._connect() as conn:
            for i in range(0, len(chunk), 400):
                batch = chunk[i : i + 400]
                marks = ",".join("?" * len(batch))
                if since:
                    sql = (
                        "SELECT code, date, open, high, low, close, volume, amount, change_pct, turnover_rate "
                        f"FROM daily_bars WHERE adjust=? AND date>=? AND code IN ({marks}) "
                        "ORDER BY code, date"
                    )
                    params: List[Any] = [adjust, since, *batch]
                elif tail:
                    sql = (
                        "SELECT code, date, open, high, low, close, volume, amount, change_pct, turnover_rate "
                        "FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rn "
                        f"      FROM daily_bars WHERE adjust=? AND code IN ({marks})) "
                        "WHERE rn<=? ORDER BY code, date"
                    )
                    params: List[Any] = [adjust, *batch, int(tail)]
                else:
                    sql = (
                        "SELECT code, date, open, high, low, close, volume, amount, change_pct, turnover_rate "
                        f"FROM daily_bars WHERE adjust=? AND code IN ({marks}) "
                        "ORDER BY code, date"
                    )
                    params = [adjust, *batch]
                for row in conn.execute(sql, params):
                    rec = dict(row)
                    out.setdefault(rec.pop("code"), []).append(rec)
        return out

    def all_codes(self) -> List[str]:
        """返回缓存中已有的全部代码。"""
        with self._connect() as conn:
            return [r["code"] for r in conn.execute(
                "SELECT DISTINCT code FROM daily_bars ORDER BY code"
            )]

    def codes_with_limit_up(
        self,
        adjust: str = "none",
        rn_min: int = 4,
        rn_max: int = 11,
        threshold: float = 0.095,
        min_bars: int = 80,
    ) -> List[str]:
        """SQL 层预筛：找出「倒数第 rn_min ~ rn_max 根内出现过涨幅 ≥threshold」的股票。

        用 9.5% 作阈值（而非精确的 10%/20%/30%），只会多选不会漏选；
        精确的涨停判定仍由 Python 侧 ``is_limit_up_bar`` 完成。
        这一步把候选池从全市场 5900 只压到几百只，是扫描提速的关键。
        """
        sql = """
        WITH t AS (
            SELECT code, close,
                   ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn,
                   LAG(close) OVER (PARTITION BY code ORDER BY date DESC) AS pc,
                   COUNT(*) OVER (PARTITION BY code) AS nb
            FROM daily_bars WHERE adjust = ?
        )
        SELECT DISTINCT code FROM t
        WHERE rn BETWEEN ? AND ? AND nb >= ? AND pc > 0 AND close >= pc * (1.0 + ?)
        ORDER BY code
        """
        with self._connect() as conn:
            return [
                r[0]
                for r in conn.execute(
                    sql, (adjust, int(rn_min), int(rn_max), int(min_bars), float(threshold))
                )
            ]

    def put_money_flow(self, code: str, rows: List[Dict[str, Any]]) -> None:
        """写入历史资金流向。"""
        if not rows:
            return
        payload = [
            (code, r["date"], r.get("main"), r.get("small"), r.get("medium"),
             r.get("large"), r.get("xlarge"), r.get("main_pct"),
             r.get("close"), r.get("chg"))
            for r in rows if r.get("date")
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO money_flow (code,date,main,small,medium,large,xlarge,"
                "main_pct,close,chg) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code,date) DO UPDATE SET main=excluded.main, small=excluded.small, "
                "medium=excluded.medium, large=excluded.large, xlarge=excluded.xlarge, "
                "main_pct=excluded.main_pct, close=excluded.close, chg=excluded.chg",
                payload,
            )

    def get_money_flow(self, code: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date, main, small, medium, large, xlarge, main_pct, close, chg "
                "FROM money_flow WHERE code=? ORDER BY date", (code,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 快速预筛

    def recent_dates(self, adjust: str = "none", n: int = 14) -> List[str]:
        """最近 n 个交易日（降序）。"""
        with self._connect() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT date FROM daily_bars WHERE adjust=? "
                    "ORDER BY date DESC LIMIT ?",
                    (adjust, int(n)),
                )
            ]

    def recent_limit_up_codes(
        self,
        adjust: str = "none",
        lookback: int = 14,
        threshold: float = 9.5,
    ) -> List[str]:
        """SQL 层预筛：最近 ``lookback`` 个交易日内涨幅 ≥ ``threshold`` 的股票。

        用 9.5% 作阈值（低于主板 10% 涨停），只会多选不会漏选；
        精确的涨停判定仍由 Python 侧 ``is_limit_up_bar`` 完成。
        这一步把候选池从全市场 5900 只压到几百只，是扫描提速的关键。
        """
        dates = self.recent_dates(adjust, lookback)
        if not dates:
            return []
        cutoff = dates[-1]
        with self._connect() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT code FROM daily_bars "
                    "WHERE adjust=? AND date>=? AND change_pct>=? ORDER BY code",
                    (adjust, cutoff, float(threshold)),
                )
            ]

    def codes_with_recent_gain(
        self, adjust: str = "none", days: int = 21, min_sum: float = 0.0
    ) -> List[str]:
        """SQL 层预筛：最近 ``days`` 个交易日累计涨幅（用 change_pct 汇总近似）≥ ``min_sum``。

        阈值故意取得很宽（默认 0），保证不会误杀；精确判定仍在 Python 侧。
        全市场扫描时先用这一步把候选从 5900 压到 2000 左右。
        """
        dates = self.recent_dates(adjust, days)
        if len(dates) < days:
            return self.all_codes()
        cutoff = dates[-1]
        with self._connect() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT code FROM daily_bars WHERE adjust=? AND date>=? "
                    "GROUP BY code HAVING SUM(change_pct) >= ? ORDER BY code",
                    (adjust, cutoff, float(min_sum)),
                )
            ]

    def mark_failed(self, code: str, adjust: str = "none") -> None:
        """记录一次“拉不到数据”，避免下次扫描反复重试。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO fetch_state (code, adjust, fetched_at, bars) VALUES (?,?,?,0) "
                "ON CONFLICT(code, adjust) DO UPDATE SET "
                "fetched_at=excluded.fetched_at, bars=0",
                (code, adjust, time.time()),
            )

    def put_bars(self, code: str, adjust: str, bars: Sequence[Dict[str, Any]]) -> None:
        if not bars:
            return
        payload = [
            (
                code,
                adjust,
                str(b.get("date")),
                b.get("open"),
                b.get("high"),
                b.get("low"),
                b.get("close"),
                b.get("volume"),
                b.get("amount"),
                b.get("change_pct"),
                b.get("turnover_rate"),
            )
            for b in bars
            if b.get("date")
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO daily_bars "
                "(code, adjust, date, open, high, low, close, volume, amount, change_pct, turnover_rate) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code, adjust, date) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume, amount=excluded.amount, "
                "change_pct=excluded.change_pct, turnover_rate=COALESCE(excluded.turnover_rate, daily_bars.turnover_rate)",
                payload,
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM daily_bars WHERE code=? AND adjust=?",
                (code, adjust),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO fetch_state (code, adjust, fetched_at, bars) VALUES (?,?,?,?) "
                "ON CONFLICT(code, adjust) DO UPDATE SET "
                "fetched_at=excluded.fetched_at, bars=excluded.bars",
                (code, adjust, time.time(), total),
            )

    def fetch_state(self, codes: Sequence[str], adjust: str) -> Dict[str, tuple]:
        """返回 ``{code: (fetched_at, bars)}``。"""
        out: Dict[str, tuple] = {}
        chunk = list(codes)
        with self._connect() as conn:
            for i in range(0, len(chunk), 500):
                batch = chunk[i : i + 500]
                marks = ",".join("?" * len(batch))
                for row in conn.execute(
                    f"SELECT code, fetched_at, bars FROM fetch_state "
                    f"WHERE adjust=? AND code IN ({marks})",
                    [adjust, *batch],
                ):
                    out[row["code"]] = (row["fetched_at"], row["bars"])
        return out

    def codes_before(
        self,
        adjust: str,
        target_date: str,
        codes: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """返回「最后一根 K 线早于 ``target_date``」的代码（即还没更新到最新交易日）。

        与 :meth:`stale_codes` 的区别：这里的判定依据是**数据本身到了哪一天**，
        而不是「上次什么时候抓的」。后者会让已经入库的股票永远不再更新。
        """
        if not target_date:
            return []
        out: List[str] = []
        chunk = list(codes) if codes is not None else None
        with self._connect() as conn:
            if chunk is None:
                for row in conn.execute(
                    "SELECT code FROM daily_bars WHERE adjust=? GROUP BY code "
                    "HAVING MAX(date) < ? ORDER BY code",
                    (adjust, target_date),
                ):
                    out.append(row[0])
                return out
            for i in range(0, len(chunk), 500):
                batch = chunk[i : i + 500]
                marks = ",".join("?" * len(batch))
                for row in conn.execute(
                    f"SELECT code FROM daily_bars WHERE adjust=? AND code IN ({marks}) "
                    f"GROUP BY code HAVING MAX(date) < ?",
                    [adjust, *batch, target_date],
                ):
                    out.append(row[0])
        return out

    def max_date(self, adjust: str = "qfq") -> str:
        """本地库里最新的交易日（YYYY-MM-DD）；空库返回 ''。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM daily_bars WHERE adjust=?", (adjust,)
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    def date_coverage(self, adjust: str = "qfq") -> Dict[str, Any]:
        """本地日线的“覆盖面”：大多数股票更新到了哪一天。

        不能用 ``MAX(date)``：只要一只股票有今天的 K 线，它就是今天，
        会造成“数据已是最新”的假象。这里取 **每股最新日期的众数**。
        """
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM daily_bars WHERE adjust=?", (adjust,)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT d, COUNT(*) c FROM ("
                "  SELECT code, MAX(date) AS d FROM daily_bars WHERE adjust=? GROUP BY code"
                ") GROUP BY d ORDER BY c DESC, d DESC LIMIT 4",
                (adjust,),
            ).fetchall()
        if not rows:
            return {"date": "", "covered": 0, "total": int(total or 0), "top": []}
        return {
            "date": str(rows[0][0]),
            "covered": int(rows[0][1]),
            "total": int(total or 0),
            "top": [[str(r[0]), int(r[1])] for r in rows],
        }

    def stale_codes(
        self,
        codes: Sequence[str],
        adjust: str = "none",
        min_bars: int = 60,
        ttl: Optional[float] = None,
    ) -> List[str]:
        """返回需要从上游补拉的代码列表。"""
        ttl = default_ttl() if ttl is None else ttl
        now = time.time()
        state = self.fetch_state(codes, adjust)
        stale: List[str] = []
        for code in codes:
            info = state.get(code)
            if info is None:
                stale.append(code)
                continue
            fetched_at, bars = info
            # 失败过的只做「短负缓存」（10 分钟）：否则一次限流会把整只股票锁死 12 小时，
            # 表现为「明明上游已有今天的数据，本地却永远不更新」。
            if bars == 0 and (now - fetched_at) <= min(ttl, 600.0):
                continue
            if bars < min_bars or (now - fetched_at) > ttl:
                stale.append(code)
        return stale

    # ------------------------------------------------------------ 键值

    def get_kv(self, key: str, ttl: Optional[float] = None) -> Optional[Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT v, ts FROM kv WHERE k=?", (key,)).fetchone()
        if not row:
            return None
        if ttl is not None and (time.time() - row["ts"]) > ttl:
            return None
        try:
            return json.loads(row["v"])
        except ValueError:
            return None

    def put_kv(self, key: str, value: Any) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO kv (k, v, ts) VALUES (?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts=excluded.ts",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    # ------------------------------------------------------------ 维护

    def stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            codes = conn.execute("SELECT COUNT(DISTINCT code) c FROM daily_bars").fetchone()["c"]
            bars = conn.execute("SELECT COUNT(*) c FROM daily_bars").fetchone()["c"]
            latest = conn.execute("SELECT MAX(date) d FROM daily_bars").fetchone()["d"]
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {
            "path": self.path,
            "codes": codes,
            "bars": bars,
            "latest_date": latest,
            "size_mb": round(size / 1024 / 1024, 2),
        }

    def clear(self, what: str = "all") -> None:
        with self._lock, self._connect() as conn:
            if what in ("all", "daily"):
                conn.execute("DELETE FROM daily_bars")
                conn.execute("DELETE FROM fetch_state")
            if what in ("all", "kv"):
                conn.execute("DELETE FROM kv")

    def prune_inactive(self, active_codes: Iterable[str]) -> int:
        """删除已退市 / 不在当前股票池中的缓存。"""
        codes = list(dict.fromkeys(active_codes))
        removed = 0
        with self._lock, self._connect() as conn:
            keep = set(codes)
            for row in conn.execute("SELECT DISTINCT code FROM daily_bars"):
                if row["code"] not in keep:
                    conn.execute("DELETE FROM daily_bars WHERE code=?", (row["code"],))
                    conn.execute("DELETE FROM fetch_state WHERE code=?", (row["code"],))
                    removed += 1
        return removed
