# 美人肩量化选股 Web 系统（2026-10-12 版本）

## 一、启动

```bash
cd /e/StockAgent
python run.py
# 或： uvicorn stock_agent.app:app --host 0.0.0.0 --port 8000
```

浏览器打开 **http://127.0.0.1:8000/mrj**

## 二、新增/改动的文件

| 文件 | 说明 |
| --- | --- |
| `stock_agent/api/mrj.py` | 美人肩 Web API（scan / detail / stats） |
| `stock_agent/api/__init__.py` | 导出 mrj_router |
| `stock_agent/app.py` | 挂载 API、`/mrj` 路由、启动预热板块指数 |
| `web/mrj.html` `mrj.css` `mrj.js` | 前端看板（ECharts K线+MACD+量能） |
| `research/02_data/28_refresh_qfq.py` | 强制把沪深主板 qfq 日线刷新到最新 |
| `stock_agent/screener/scanner.py` | `ensure_bars(force=True)`、`scan_second_board(refresh=)` |
| `stock_agent/screener/context.py` | 板块指数只取近 120 交易日（首次扫描 88s → 12s） |
| `stock_agent/screener/patterns.py` | 日期→ISO周 键缓存（去掉 8 万次 strptime） |

## 三、API

```
GET /api/mrj/stats                      策略统计（回测口径）
GET /api/mrj/scan?market=main&limit=40  扫描候选
      参数：min_gap, max_gap, close_band, max_amp, min_vol, anchor_min_gain,
            refresh=1（忽略结果缓存，快）, force_bars=1（重新拉日线，慢）
GET /api/mrj/detail?code=002349         K线+指标+作战手册+契合度体检
```

## 四、页面功能

* 顶部：策略五年回测统计（选中 8316 次 / 二板成功 1447 次 / 兑现率 17.4%）
* 过滤器：蓄势天数、日均振幅、量能趋势、沪深主板/全部/沪市/深市、关键词搜索
* 左栏：候选卡片（蓄势天数 / 日均振幅 / 量能趋势 / 匹配得分 / 二次涨停概率）
* 右栏：K线图（含首板标记、横盘蓄势区、MA5/10/20、成交量、MACD）
      + 形态节点诊断 + 实战潜伏作战手册 + 量化特征契合度体检

## 五、数据刷新（重要）

扫描用的是本地 `qfq` 日线。数据旧了要刷新：

```bash
python research/02_data/28_refresh_qfq.py          # 全市场（约 24 分钟）
```

或点页面上的「⟳ 刷新日线」（只刷候选池，约 1~2 分钟）。

## 六、当前策略参数（大道至简 + 三轮优化）

```
anchor_min_gain = 9.5   # 锚点：涨停/准涨停
anchor_max_gain = 10.5
min_gap = 3, max_gap = 15     # 横盘 3~15 天
close_band = 0.07             # 收盘 ±7%
min_vol_trend = 0.8           # 量能趋势 ≥0.8
max_range_pct = 0.15          # 总振幅 ≤15%
market = main                 # 沪深主板
```

五年回测（2021-09-17 ~ 2026-09-17，沪深主板 3181 只）：
信号精度 10.7% / 事件精度 28.2% / 召回 20.3% / 日均候选 14.2 只。
持有到被剔除：二次涨停 17.4%、冲高突破 26.0%、跌破下沿 39.5%、超时 17.1%。
