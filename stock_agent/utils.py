"""通用工具：股票代码归一化、交易时段判断、数值清洗。"""

from __future__ import annotations

import math
import re
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Iterable, List, Optional

# ---------------------------------------------------------------- 时区 / 时间

CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# ---------------------------------------------------------------- 市场常量

MK_SH = 1  # 上海证券交易所
MK_SZ = 0  # 深圳证券交易所
MK_BJ = 0  # 北京证券交易所（东方财富同样使用 0）

_CODE_RE = re.compile(r"(\d{6})")

# A 股交易时段（北京时间）
CALL_AUCTION_OPEN = (dtime(9, 15), dtime(9, 25))   # 开盘集合竞价
MORNING_SESSION = (dtime(9, 30), dtime(11, 30))    # 上午连续竞价
AFTERNOON_SESSION = (dtime(13, 0), dtime(15, 0))   # 下午连续竞价
CLOSING_AUCTION = (dtime(14, 57), dtime(15, 0))    # 收盘集合竞价


def now_cn() -> datetime:
    """当前北京时间。"""
    return datetime.now(CN_TZ)


# ---------------------------------------------------------------- 代码处理


def normalize_code(code: Any) -> str:
    """从任意写法中提取 6 位证券代码。

    支持 ``600519`` / ``sh600519`` / ``SH.600519`` / ``600519.SH`` / ``1.600519``。
    """
    if code is None:
        raise ValueError("证券代码不能为空")
    text = str(code).strip().upper()
    if not text:
        raise ValueError("证券代码不能为空")
    if "." in text:
        head, _, tail = text.partition(".")
        for part in (head, tail):
            m = _CODE_RE.search(part)
            if m:
                return m.group(1)
    m = _CODE_RE.search(text)
    if not m:
        raise ValueError(f"无法解析证券代码: {code!r}")
    return m.group(1)


def guess_market(code: Any) -> int:
    """根据代码前缀推断东方财富市场号（1=沪市，0=深市/北交所）。"""
    digits = normalize_code(code)
    head = digits[0]
    if head in ("6", "5", "9", "7"):
        return MK_SH
    if head == "1":
        # 15xxxx/16xxxx/18xxxx/19xxxx 为深市基金与债券，其余（110/113/118/132…）为沪市
        return MK_SZ if digits.startswith(("15", "16", "18", "19")) else MK_SH
    # 0xxxxx 深主板 / 2xxxxx 深B / 3xxxxx 创业板 / 4xxxxx·8xxxxx 北交所
    return MK_SZ


def to_secid(code: Any, market: Optional[int] = None) -> str:
    """把各种写法统一成东方财富 secid，如 ``1.600519``。"""
    text = str(code).strip().upper()
    if "." in text:
        head, _, tail = text.partition(".")
        if head in ("0", "1") and tail.isdigit():
            return f"{head}.{tail}"
        if tail.isdigit() and head.isdigit():
            return f"{head}.{tail}"
        if tail in ("SH", "SS", "SZ", "BJ", "SHSE", "SZSE", "BSE"):
            mk = MK_SH if tail in ("SH", "SS", "SHSE") else MK_SZ
            return f"{mk}.{normalize_code(text)}"
    mk = market if market is not None else guess_market(text)
    return f"{mk}.{normalize_code(text)}"


def split_secid(secid: str) -> tuple:
    """``1.600519`` -> ``(1, "600519")``。"""
    head, _, tail = str(secid).partition(".")
    return int(head) if head.isdigit() else 0, tail


def market_name(secid_or_code: Any) -> str:
    mk = guess_market(secid_or_code) if "." not in str(secid_or_code) else int(str(secid_or_code).split(".")[0])
    return {MK_SH: "SH", MK_SZ: "SZ"}.get(mk, "SZ")


def with_prefix(code: Any) -> str:
    """``600519`` -> ``sh600519``（新浪风格）。"""
    mk = guess_market(code)
    return ("sh" if mk == MK_SH else "sz") + normalize_code(code)


# ---------------------------------------------------------------- 交易时段


def market_phase(dt: Optional[datetime] = None) -> str:
    """返回当前市场阶段。

    取值: ``pre_open`` / ``call_auction`` / ``trading`` / ``lunch_break`` /
    ``closed`` / ``weekend``
    """
    dt = dt or now_cn()
    if dt.weekday() >= 5:
        return "weekend"
    t = dt.time()
    if t < CALL_AUCTION_OPEN[0]:
        return "pre_open"
    if CALL_AUCTION_OPEN[0] <= t < CALL_AUCTION_OPEN[1]:
        return "call_auction"
    if t < MORNING_SESSION[0]:
        return "pre_open"
    if MORNING_SESSION[0] <= t < MORNING_SESSION[1]:
        return "trading"
    if t < AFTERNOON_SESSION[0]:
        return "lunch_break"
    if AFTERNOON_SESSION[0] <= t <= AFTERNOON_SESSION[1]:
        return "trading"
    return "closed"


PHASE_LABEL = {
    "pre_open": "盘前",
    "call_auction": "集合竞价",
    "trading": "交易中",
    "lunch_break": "午间休市",
    "closed": "已收盘",
    "weekend": "休市",
}


def is_trading(dt: Optional[datetime] = None) -> bool:
    return market_phase(dt) in ("trading", "call_auction")


# ---------------------------------------------------------------- 数值清洗


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """东方财富用 ``"-"`` 表示缺失值。"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return default
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "--", "null", "None", "nan"):
        return default
    try:
        return float(text)
    except ValueError:
        return default


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    f = safe_float(value)
    return int(f) if f is not None else default


def chunked(items: Iterable[Any], size: int) -> List[list]:
    """把一个可迭代对象切成固定大小的块。"""
    buf: List[Any] = []
    out: List[list] = []
    for it in items:
        buf.append(it)
        if len(buf) >= size:
            out.append(buf)
            buf = []
    if buf:
        out.append(buf)
    return out


def round_or_none(value: Any, digits: int = 2) -> Optional[float]:
    f = safe_float(value)
    return None if f is None else round(f, digits)
