"""数据源集合。"""

from .base import BaseProvider, DataSourceError, Quote
from .eastmoney import EastMoneyProvider
from .sina import SinaProvider
from .tencent import TencentProvider

__all__ = [
    "BaseProvider",
    "DataSourceError",
    "Quote",
    "EastMoneyProvider",
    "SinaProvider",
    "TencentProvider",
]
