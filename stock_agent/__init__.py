"""StockAgent —— A 股实时行情 / 分时 / K 线数据服务。

数据来源（免费、无需 token）：
  * 东方财富 push2 接口（主）
  * 新浪财经 hq.sinajs.cn（备用实时报价）

用法::

    from stock_agent import MarketService

    async with MarketService() as svc:
        q = await svc.quote("600519")
        print(q["name"], q["price"])
"""

from .config import Settings, get_settings
from .service import MarketService

__version__ = "0.1.0"
__all__ = ["MarketService", "Settings", "get_settings", "__version__"]
