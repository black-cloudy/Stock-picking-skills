# StockAgent · A 股实时行情服务

免费、无需 token 的 **A 股实时行情 / 分时 / K 线** 数据服务 + 同花顺风格 Web 界面。

数据源全部为公开接口，开箱即用：

| 数据源 | 覆盖能力 | 说明 |
| --- | --- | --- |
| **东方财富** `push2*.eastmoney.com` | 实时行情、分时、日/周/月/分钟 K 线、排行、搜索、逐笔 | 主数据源，字段最全，自动切换镜像主机 |
| **腾讯财经** `qt.gtimg.cn` / `ifzq.gtimg.cn` / `stock.gtimg.cn` | 实时行情 + **五档盘口**、分时、日/周/月/分钟 K 线、**逐笔明细**、拼音搜索 | 主备（3 个备用域名） |
| **新浪财经** `hq.sinajs.cn` / `quotes.sina.cn` / `vip.stock.finance.sina.com.cn` | 实时行情 + **五档盘口**、**日/分钟 K 线（仅不复权）**、**全市场列表** | 兜底（东财限流时接管排行榜与股票池） |

> 三个数据源组成故障转移链，任一来源异常自动降级并在日志中记录，**接口不会挂**。
>
> 全市场列表 / 排行榜链路：**东财 → 新浪 → 本地缓存**（三级）。东财被限流时，
> 新浪分页接口自动接管；上游全挂时直接用本地日线库的代码 + 名称兜底，扫描仍可运行。
>
> 逐笔成交链路：**东财 → 腾讯**（腾讯明细分页接口，二分定位末页）。

---

## 项目结构

```
StockAgent/
├── run.py / start.bat / start.sh   启动入口
├── requirements.txt
├── README.md                       本文档
├── stock_agent/                    核心代码
│   ├── providers/                  行情数据源（东财 / 腾讯 / 新浪）
│   ├── screener/                   选股引擎
│   │   ├── patterns.py             美人肩形态判定（SecondBoardConfig）
│   │   ├── trend.py                趋势票模板匹配
│   │   ├── scanner.py              扫描调度
│   │   ├── context.py              大盘 / 板块环境
│   │   └── store.py                本地缓存（SQLite）
│   ├── api/                         HTTP 接口
│   │   ├── routes.py               行情 API
│   │   └── mrj.py                  美人肩 API
│   └── app.py                      FastAPI 装配
├── web/                            前端
│   ├── index.html                  实时行情看板（/）
│   ├── mrj.html                    美人肩量化看板（/mrj）
│   ├── backtest_classic.html       经典口径 30 万回测（/static/backtest_classic.html）
│   └── backtest_broad.html         广义口径 30 万回测（/static/backtest_broad.html）
├── tests/                          冒烟测试
├── tools/                          运维脚本
│   ├── daily_scan.py               每日巡检（经典+广义，有票提醒）
│   ├── start_scan.bat              巡检入口（刷新+扫描+弹窗）
│   └── register_task.bat           注册 Windows 计划任务
├── research/                       研究脚本（详见 research/README.md）
│   ├── 01_meirenjian/              美人肩核心研究（挖样本 / 特征 / 规则标定）
│   ├── 02_data/                    数据抓取与刷新（不复权日线 / 同花顺历史）
│   ├── 03_backtest/                回测与报告（30 万组合回测 / HTML 报告）
│   ├── 04_trend/                   趋势票（涨不停不涨停）
│   ├── 05_lowpos/                  低位票（低位均线粘合）
│   └── _archive/                   已被取代的实验脚本（不参与运行）
├── docs/                           文档
│   ├── WEB_README.md               网页看板与 API 说明
│   └── archive/                    历史研究结论快照
└── .data/                          数据缓存（不入库，可重跑生成）
    ├── screener.db                 日线缓存（q 复权，约 2.3 GB）
    ├── raw_bars.db                 不复权日线（与行情软件一致，约 530 MB）
    ├── sim_raw_results.json        30 万回测结果
    └── 美人肩_30万回测报告.html      交易明细报告
```

> `.data/` 与 `research/` 下的大文件均可由脚本重新生成，详见各脚本头部注释。

---

## 快速开始

```bash
# 1) 安装依赖
pip install -r requirements.txt

# 2) 启动服务
python run.py

# 打开浏览器
#   Web 界面   http://127.0.0.1:8000/
#   接口文档   http://127.0.0.1:8000/docs
```

Windows 下也可以直接双击 `start.bat`。

自定义端口 / 对外提供：

```bash
python run.py --port 9000 --host 0.0.0.0
```

---

## Web 界面功能

打开 `http://127.0.0.1:8000/` 即可看到：

- **顶部大盘指数** —— 上证 / 深成 / 创业板 / 沪深300 / 科创50 / 北证50 / 中证500，实时刷新
- **左侧搜索** —— 支持 代码 / 名称 / 拼音首字母，下拉即选
- **自选股** —— 本地保存（localStorage），涨红跌绿，实时跳动；`✕` 或右键移除
- **排行榜** —— 全部A股 / 沪 / 深 / 创业板 / 科创板 / 北交所 / ETF / 指数，按涨跌幅、成交额、换手率、量比、主力净流入、总市值排序
- **分时图** —— 价格线 + 均价线 + 昨收基准线 + 分钟成交量，右轴自动换算涨跌百分比
- **五日分时** —— 最近 5 个交易日连续分时
- **K 线图** —— 1 / 5 / 15 / 30 / 60 分钟、日 K、周 K、月 K；MA5/10/20/60 均线；成交量；前复权 / 后复权 / 不复权；支持缩放拖拽
- **五档盘口** —— 买卖各五档价格 + 挂单量，带量能条
- **逐笔成交** —— 时间、价格、手数、金额、主动买卖方向
- **关键指标** —— 均价、量比、涨跌停、市盈率、市净率、总/流通市值、委比委差、每股净资产、主力净流入

---

## 目录结构

```
StockAgent/
├── run.py                     # 启动入口
├── requirements.txt
├── start.bat / start.sh       # 一键启动
├── stock_agent/
│   ├── config.py              # 配置
│   ├── utils.py               # 代码归一化 / 交易时段 / 数值清洗
│   ├── cache.py               # TTL 缓存（单飞防击穿）
│   ├── service.py             # 业务层 + 数据源熔断/故障转移
│   ├── cli.py                 # 行情命令行工具
│   ├── app.py                 # FastAPI 装配
│   ├── api/routes.py          # HTTP / WebSocket 路由
│   ├── providers/
│   │   ├── base.py            # 数据源接口
│   │   ├── eastmoney.py       # 东方财富（主，含涨停池）
│   │   ├── tencent.py         # 腾讯财经（备，3 个备用域名）
│   │   └── sina.py            # 新浪财经（兜底 / 五档）
│   └── screener/              # ★ 选股器
│       ├── store.py           # SQLite 日线缓存
│       ├── indicators.py      # 涨停价 / 均线 / ST 判定
│       ├── strategies.py      # 美人肩 + 涨不停不涨停
│       ├── lowpos.py          # 低位票（低位均线粘合）
│       ├── scanner.py         # 并发扫描调度
│       └── cli.py             # python -m stock_agent.screener
├── web/                       # 前端（原生 JS + ECharts，无构建）
│   ├── index.html / style.css / app.js
│   └── vendor/echarts.min.js
├── .pi/skills/                # ★ pi 技能
│   ├── meirenjian-classic/SKILL.md   经典美人肩（涨停-横盘-涨停）
│   ├── meirenjian-broad/SKILL.md     广义美人肩（大涨-横盘-涨停）
│   ├── steady-climb-scan/SKILL.md
│   └── lowpos-scan/SKILL.md
├── .data/screener.db          # 选股器本地日线缓存（自动生成）
└── tests/test_smoke.py        # 冒烟测试（需联网）
```

---

## 选股器（找票）

内置多个形态策略，`python -m stock_agent.screener` 直接跑。

### ① 美人肩（二次涨停）—— 首板 → 收盘震幅 ±3% 整理 3~10 天 → 二次涨停

```bash
python -m stock_agent.screener meirenjian --top 30      # 别名: secondboard / mrj / 二板
python -m stock_agent.screener meirenjian --close-band 0.04   # 放宽震幅到 ±4%
python -m stock_agent.screener meirenjian --market all        # 全市场（默认只扫沪深主板）
python -m stock_agent.screener meirenjian --min-score 0.50    # 只要高分候选
```

**默认口径**：仅深交所 + 每天收盘价在首板价 **±3%** 内 + 整理 **3~10 天**（超过 10 天放弃）+ 60 日涨停次数 ≥5。

判定条件由 **31 万个样本**（2019-2026 全市场 477 万根前复权日线）统计得出：

| 规律 | 数据 |
| --- | --- |
| 整理 **越短越好**（上限 10 天） | gap=2 → 9.31%，gap=6 → 4.77%，gap=10 → 3.24%，gap=12 → 3.10% |
| **60 日涨停次数 ≥5**（最强因子） | AUC 0.687，正样本中位 5 次 vs 负样本 3 次 |
| 量能**撑住不能缩量** | AUC 0.622，正样本量能趋势中位 0.854 vs 负 0.668 |
| 震幅收窄会降低命中率 | ±3% → 7.09%，±4% → 7.15%，不限制 → 8.47% |
| 股价偏低更好 | AUC 0.389，正样本中位 5.8 元 vs 负 9.1 元 |
| 首板不要巨量 | AUC 0.394，正样本放量 1.36 倍 vs 负 1.75 倍 |

输出按打分排序，并给出每个档位的**历史启爆概率**（基准 4.8%）：

```
#   代码    名称      打分  启爆概率  现价   首板价  首板日      整理  60日涨停  距首板%  收盘下偏%  收盘上偏%  量比  量能趋势
1  002068  黑猫股份  73.3     14.3%  10.15   10.01  2026-09-09     3         6    +1.40      -3.30      +2.90  0.96      0.79
```

> **命中不足会自动放宽**：±3% 较严，当天可能选不出票。脚本会依次放宽到
> 涨停次数≥3 → ±4% → ±5% → ±6% → 不限，并用 `⚠` 明确提示实际生效的参数。

> ⚠️ **执行要点（研究结论）**：筛选出的票**次日平均低开 0.43%**，
> 「T+0 收盘买 → T+1 收盘卖」期望 **-0.74%**；正确姿势是**次日开盘观察后买入**
> （「T+1 开盘买 → T+1 收盘卖」期望 **+0.60%**）。
> 一旦封板成功，**3 日内最高价中位数 +15.77%**，不要急于平掉；
> 跌破首板价 5%~8% 果断止损。这是提高命中率的工具，不是稳赚策略。

**扫描耗时 3.8 秒**（全市场缓存 620MB）：SQL 层用 `change_pct>=9.5` + 日期索引把候选池
从 5896 只压到几百只，只对候选股计算 MA/MACD。

### ② 趋势票（涨不停不涨停）—— 连续 15~30 天小步上涨、回调极浅

```bash
python -m stock_agent.screener trend --top 30                  # 别名: steady / ztbt / 涨不停 / 趋势票
python -m stock_agent.screener trend --max-distance 1.50       # 只要最像的（每日约0.2只）
python -m stock_agent.screener trend --max-distance 2.30       # 放宽（每日约3.4只）
python -m stock_agent.screener trend --market all              # 改看全市场（默认只扫沪深主板）
```

**判定方式**：以用户提供的两个真实样本为模板，做 **14 维标准化相似度匹配**
（不是手工阈值）。**默认只扫沪深主板**（排除创业板/科创板）。

| 样本 | 区间 | 涨幅 | 关键特征 |
| --- | --- | --- | --- |
| 002990 盛视科技 | 2026-03-13 → 04-13 | +32% | 最大回撤 3.2%、红柱占比 100%、贴近 60 日新高 |
| 002655 共达电声 | 2026-03-24 → 05-06 | +118% | 最大回撤 1.7%、均线 100% 多头、涨日量>跌日量 |

> ⚠️ **先说没用的**：教科书条件（均线多头 + MACD 零轴上 + 平滑 + 不过热）
> **全部跑输基准**（+2.0% vs 基准 +2.41%，上涨率 50.9% vs 53.3%）。因为它们本质是追涨，
> 均值回归吃掉了动量。**唯一有效的是"与这两个样本的相似度"**。

**25 万样本验证**（距离越小 → 摸到 +15% 的概率越高，单调）：

| 距离 | 每日候选 | 20 日 | **摸到 +15%** |
| --- | --- | --- | --- |
| ≤1.50 | ~0.2 只 | +2.1% | 42.9% |
| **≤1.90**（默认） | **~1.0 只** | **+2.8%** | **43.8%** |
| ≤2.30 | ~3.4 只 | +2.7% | 41.2% |
| **基准** | 244 只 | +2.0% | **29.1%** |

```bash
# 今天的输出
python -m stock_agent.screener trend --top 15
# （2026-09-14 唯一命中 301390 经纬股份 因停牌被排除，当日为空）
```

> ⚠️ **这是高赔率、低胜率形态**：摸 +15% 概率 43%（基准 29%），但**上涨率仅 48%**，
> **预期 20 日最大回撤 -10%**（基准 -8%）。靠赔率不是靠胜率，务必控制仓位。
> 破位信号：跌破 MA20 或 MACD 柱转绿。
>
> **没有自动放宽** —— 这类票稀少（平均每日 0~3 只），**空结果是正常现象**。

**扫描耗时约 3 秒。**

### ③ 低位票（低位均线粘合 → 拐头 / 突破）—— 前期暴跌后在低位均线接近重合

```bash
python -m stock_agent.screener lowpos --top 30                 # 默认：拐头 + 突破都扫（别名: 低位 / 低位票 / 均线粘合）
python -m stock_agent.screener lowpos --mode early --top 30     # 只看拐头（历史有超额，适合按策略交易）
python -m stock_agent.screener lowpos --mode breakout --top 30  # 只看突破（看图确认，不建议无脑追）
python -m stock_agent.screener lowpos --market all              # 全市场（默认只扫沪深主板）
```

**用户样本（8 个）**：

* 拐头档：605366 宏柏新材、002990 盛视科技（低位粘合 + MA5 刚上穿 MA10）；
* 突破档：000573 粤宏远A、000607 华媒控股、003029 吉大正元、000995 皇台酒业、
  600199 金种子酒、603999 读者传媒（低位平台 + 放量大阳/涨停突破）。

判定规则（**默认沪深主板**，排除创业板/科创板、ST、股价 < 2 元）：

| 条件 | 拐头（early） | 突破（breakout） |
| --- | --- | --- |
| 前期暴跌 | 250 日回撤 ≥ 30%、120 日回撤 ≥ 35% | 250 日回撤 ≥ 30% |
| 低位 | 250 日位置 ≤ 45%、距 250 日高 ≤ 25% | 250 日位置 ≤ 65%、距 250 日高 ≤ 13% |
| 均线粘合 | 近 10 日最小离散度 ≤ 2.5% | 近 20 日最小离散度 ≤ 3.0% |
| 拐头/启动 | 5 日内 MA5 上穿 MA10、BIAS20 ≤ 8% | 当日 ≥4% 或 3 日 ≥8%、MA5>MA10、BIAS20 ≤ 20% |
| 量能 | 可选 `--min-vol-trend 1.1` | 今日量/前5日均量 ≥ 0.7（一字板豁免） |
| MACD | 柱 ≥ 0 | 柱 ≥ -0.005 |

**信号级验证**（2021-01 ~ 2026-10，不复权，次日开盘买入，20 日净收益）：

| 模式 | 样本 | 20 日净收益 | 胜率 | 结论 |
| --- | --- | --- | --- | --- |
| **拐头** | 99,661 | **+1.41%** | 51.0% | 有正超额，适合按策略交易 |
| 突破 | 70,056 | +0.03% | 43.4% | 看图确认可以，机械追突破没有超额 |

**30 万组合回测（优化版）**：拐头 + **中证1000 > MA20** 才买 + 排序（低位置 + 近20日弱 + 低价格 + **小市值 + 低成交额**）
+ 流动性下限 0.2 亿/日 + 最多 **4 只** + 20 日到期 + **+25% 止盈 / −15% 止损**。

| 指标 | 优化版 | 旧版 |
| --- | --- | --- |
| 期末权益 | **1,454,615**（+384.87%） | 714,881（+138.29%） |
| 年化 / 最大回撤 | **+31.41% / −19.21%** | +16.21% / −40.41% |
| 交易 / 胜率 / PF | 221 笔 / 55.2% / 1.93 | 287 笔 / 50.2% / 1.49 |
| 训练 2021-2023 / 验证 2024-2026 | **+99.9% / +141.9%** | +20.9% / +74.6% |
| 分年度 | 2021 +60.7%、2022 +15.8%、2023 +7.4%、2024 +42.0%、2025 +41.6%、2026 +20.7% | — |

> ⚠️ **诚实结论**：优化版收益主要来自**小市值 + 低流动性**因子（2021-2023 小市值行情最强），
> 这类股票容量有限、滑点更大；回测未额外计滑点，实盘会更差。排名/止损/持有期都是样本内选的，
> 2023 年只赚 7%，未来可能失效。突破档的 6 个新样本能被扫出来，但「次日开盘追突破」的 20 日净收益
> 只有 +0.03%——**看图形确认可以，别当买入信号**。逐笔报告见 **`低位票_30万回测_优化版.html`**。

**扫描耗时约 20 秒**（全市场读取 300 根日线 + 本地指标计算）。

### 预热缓存（推荐，只需跑一次）

```bash
python -m stock_agent.screener warmup --concurrency 32
```

把全市场约 5900 只股票的日线落到 `.data/screener.db`（约 60 MB，1~2 分钟）。
之后所有扫描**秒级出结果**，并且几乎不再打上游接口。缓存盘中 5 分钟有效、盘后长期有效，
次日自动增量更新。

### 其他命令

```bash
python -m stock_agent.screener stats                 # 查看本地缓存
python -m stock_agent.screener clear --what daily    # 清空日线缓存
python -m stock_agent.screener meirenjian --json -o mrj.json
python -m stock_agent.screener steady --csv -o steady.csv
```

> **自动放宽**：命中为空时脚本会依次放宽波动幅度 / 回撤 / 观察天数等，
> 并在结果下方用 `⚠` 标注实际生效的参数。想严格按参数执行加 `--no-auto-relax`。

### pi 技能

项目内置四个 [pi skill](.pi/skills/)：

| Skill | 触发语 | 形态 |
| --- | --- | --- |
| `meirenjian-classic` | 「找经典美人肩 / 找二板」 | 涨停（≥9.4%）→ 横盘 → 二次涨停 |
| `meirenjian-broad` | 「找广义美人肩 / 大涨后横盘」 | 大涨（4~9.4%）→ 横盘 → 涨停 |
| `steady-climb-scan` | 「找涨不停不涨停的票」 | 稳步攀升趋势 |
| `lowpos-scan` | 「找低位票 / 均线粘合 / 低位突破」 | 暴跌后低位均线粘合拐头 + 放量突破 |

---

## 每日自动巡检

每天收盘后自动扫一遍，输出**两栏**：

| 栏目 | 内容 | 能不能直接做 |
| --- | --- | --- |
| **① 严格口径** | 经典 + 广义，参数与回测完全一致 | ✅ 可执行 |
| **② 观察池** | 参数主动放宽（横盘3~10天、震幅±5%、日振幅≤7%、量能≥0.8），每条标注**偏离了哪一项** | ❌ 仅供观察 |

```bash
# 手动跑一次（用现有缓存，约 10 秒）
python tools/daily_scan.py

# 先刷新日线再扫（约 20~25 分钟）
python tools/daily_scan.py --refresh --popup
```

**注册成 Windows 计划任务（每天 16:10）**：双击运行 `tools\register_task.bat`（需管理员权限）。

| 命令 | 作用 |
| --- | --- |
| `schtasks /Query /TN "StockAgent_Meirenjian" /V /FO LIST` | 查看详情 |
| `schtasks /Run /TN "StockAgent_Meirenjian"` | 立即执行一次 |
| `schtasks /Delete /TN "StockAgent_Meirenjian" /F` | 删除任务 |

**输出**：
- `.data/alerts/YYYY-MM-DD.md` —— 当天巡检报告（Markdown 表格）
- `.data/alerts/latest.md` / `latest.json` —— 最新一次结果
- `.data/alerts/cron.log` —— 计划任务运行日志

---

## Python 调用

```python
import asyncio
from stock_agent import MarketService

async def main():
    async with MarketService() as svc:
        # 实时行情（含五档）
        q = await svc.quote("600519", with_orderbook=True)
        print(q["name"], q["price"], q["change_pct"], "%")

        # 分时（1 日 / 5 日）
        m = await svc.minute("000001", days=5)
        print(m["count"], "个点")

        # K 线：1m/5m/15m/30m/60m/daily/weekly/monthly
        k = await svc.kline("600519", period="daily", adjust="qfq", limit=250)
        for bar in k["bars"][-3:]:
            print(bar["date"], bar["open"], bar["close"], bar["volume"])

        # 批量行情
        rows = await svc.quotes(["600519", "000001", "300750"])

        # 排行榜 / 搜索
        rank = await svc.stock_list(page=1, page_size=20, sort_field="pct", market="cyb")
        hits = await svc.search("茅台")

        # 一次拿全（行情 + 五档 + 分时 + K 线 + 逐笔），并行请求
        snap = await svc.snapshot("600519", period="daily")

asyncio.run(main())
```

> 代码写法很宽松：`600519`、`sh600519`、`600519.SH`、`1.600519` 都支持；
> 北交所（`4xxxxx` / `8xxxxx` / `920xxx`）自动识别。

---

## 命令行工具

```bash
python -m stock_agent.cli quote 600519 --book      # 实时行情 + 五档
python -m stock_agent.cli minute 600519 --days 1   # 分时
python -m stock_agent.cli kline 600519 -p daily -n 20
python -m stock_agent.cli rank --market cyb --sort pct
python -m stock_agent.cli search 茅台
python -m stock_agent.cli indices
python -m stock_agent.cli status
python -m stock_agent.cli watch 600519 000001 300750 --interval 3   # 终端盯盘
```

加 `--json` 输出原始 JSON。

---

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/market/status` | 交易时段 / 数据源 / 缓存统计 |
| GET | `/api/market/indices` | 大盘指数实时行情 |
| GET | `/api/stocks` | 股票列表 / 排行榜，参数 `page` `page_size` `sort_field` `order` `market` |
| GET | `/api/search?q=` | 搜索证券（代码 / 名称 / 拼音） |
| GET | `/api/batch/quotes?codes=` | 批量实时行情（逗号分隔，最多 200 只） |
| GET | `/api/stock/{code}/quote` | 实时行情，`?orderbook=true` 附带五档 |
| GET | `/api/stock/{code}/orderbook` | 五档盘口 |
| GET | `/api/stock/{code}/minute` | 分时，`?days=1..5` |
| GET | `/api/stock/{code}/kline` | K 线，`?period=&adjust=&limit=&start=&end=` |
| GET | `/api/stock/{code}/ticks` | 逐笔成交明细 |
| GET | `/api/stock/{code}/snapshot` | 聚合快照（行情+五档+分时+K线+可选逐笔） |
| WS | `/ws/quotes` | 实时行情推送 |

`period` 取值：`1m` `5m` `15m` `30m` `60m` `daily` `weekly` `monthly`
`adjust` 取值：`qfq`（前复权，默认） `hfq`（后复权） `none`（不复权）

### 示例

```bash
curl "http://127.0.0.1:8000/api/stock/600519/quote?orderbook=true"
curl "http://127.0.0.1:8000/api/stock/600519/kline?period=daily&adjust=qfq&limit=120"
curl "http://127.0.0.1:8000/api/stocks?market=cyb&sort_field=amount&page_size=20"
```

### WebSocket 订阅

```javascript
const ws = new WebSocket('ws://127.0.0.1:8000/ws/quotes');
ws.onopen = () => ws.send(JSON.stringify({
  action: 'subscribe',
  codes: ['600519', '000001', '300750'],
}));
ws.onmessage = (e) => console.log(JSON.parse(e.data));
// 支持 action: subscribe / pause / resume / ping
```

---

## 配置

全部通过环境变量覆盖（见 `stock_agent/config.py`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STOCK_HOST` / `STOCK_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `STOCK_HTTP_TIMEOUT` | `8` | 上游超时（秒） |
| `STOCK_HTTP_RETRIES` | `2` | 上游重试次数 |
| `STOCK_MAX_CONCURRENCY` | `16` | 最大并发请求数 |
| `STOCK_TTL_QUOTE` | `3` | 行情缓存 TTL（秒） |
| `STOCK_TTL_MINUTE` | `10` | 分时缓存 TTL |
| `STOCK_TTL_KLINE` | `60` | K 线缓存 TTL |
| `STOCK_TTL_LIST` | `20` | 列表缓存 TTL |
| `STOCK_SINA_FALLBACK` | `true` | 是否启用新浪兜底 |
| `STOCK_SOURCE_ORDER` | 空 | 强制数据源优先级，如 `tencent,eastmoney` |
| `STOCK_BREAKER_THRESHOLD` | `3` | 某源连续失败多少次后熔断 |
| `STOCK_BREAKER_COOLDOWN` | `60` | 熔断冷却时间（秒） |
| `STOCK_DATA_DIR` | `./.data` | 本地缓存目录（选股器日线库） |
| `STOCK_WS_INTERVAL` | `3` | WebSocket 推送间隔（秒） |
| `STOCK_LOG_LEVEL` | `info` | 日志级别 |

---

## 测试

```bash
python tests/test_smoke.py
```

覆盖：代码归一化、交易时段判断、实时行情、五档盘口、大盘指数、1 日/5 日分时、
日/周/月/分钟 K 线、K 线翻页、批量行情、排行榜、板块筛选、搜索、逐笔成交、聚合快照。

---

## 说明与注意事项

- **数据延迟**：东方财富首选主机被限流时会自动切换到腾讯；若排行榜落到
  `push2delay` 镜像，接口会返回 `"delayed": true` 标记（延时约 15 分钟）。
- **交易日历**：`market_phase()` 只判断周末与时段，未内置法定节假日日历。
  如需精确判断，可在 `stock_agent/utils.py` 中接入交易日历。
- **K 线上限**：东方财富单次可翻页至 5000 根；腾讯备用源单次约 800 根（日/周/月）、
  320 根（分钟）。
- **数据源限流**：东方财富 `push2his` 与腾讯 `web.ifzq` 在短时间大量请求后会被临时限流。
  服务层已做三重保障：① 每源连续失败 3 次自动熔断 60 秒；② 腾讯内置 3 个备用域名；
  ③ 选股器把日线落到本地 SQLite，**同一交易日只拉一次**。
  建议首次使用先跑 `warmup`，之后扫描不再打上游。
- **涨停池保留期**：东方财富涨停板接口仅保留最近约 20 个自然日，
  因此 `美人肩` 的 `--lookback` 超过约 14 个交易日时建议改用 `--universe all`（走本地缓存）。
- **策略仅供参考**：形态选股不构成任何投资建议，请自行结合基本面与仓位管理决策。
- **合规提醒**：本项目仅用于个人学习与研究，数据版权归各数据源所有，
  请勿用于商业用途或高频抓取。
