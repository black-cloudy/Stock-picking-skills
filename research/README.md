# research · 研究脚本索引

所有脚本都可从项目根目录直接运行，路径引导已做自动探测（放在任意子目录都能找到 `stock_agent`）。

```bash
python research/03_backtest/35_sim_raw.py      # 例：30 万组合回测
```

## 01_meirenjian · 美人肩核心研究

| 脚本 | 作用 |
| --- | --- |
| `01_fetch_deep.py` | 抓取深度特征（换手/市值等） |
| `02_mine.py` | 挖掘「首板→整理→二次涨停」全量样本，导出 CSV |
| `03_analyze.py` | 样本分布与基础统计 |
| `04_rules.py` | 规则筛选取值扫描 |
| `05_expectancy.py` | 期望值 / 盈亏比测算 |
| `06_exit.py` | 出场规则研究（止损/持有期） |
| `07_calibrate.py` | 打分→概率标定表 |
| `08_tune.py` | 参数调优 |
| `16_fail_reasons.py` | 失败案例归因 |
| `17_avoid_fail.py` | 失败规避六条规则标定 |
| `18_event_study.py` | 事件研究（涨停后各日表现） |
| `19_mild_anchor.py` | 温和锚点（4~9.5%）分支标定 |
| `21_deep_features.py` | 深层特征（周K/换手/筹码） |
| `23_context_analysis.py` | 大盘 / 板块环境特征分析 |

## 02_data · 数据抓取与刷新

| 脚本 | 作用 |
| --- | --- |
| `15_fetch_meta.py` | 抓流通股本等元数据 → `.data/meta.json` |
| `20_fetch_ths.py` | 同花顺 6 年历史日线（含换手率） |
| `22_fetch_context.py` | 上证指数 + 行业映射 → `.data/market_ctx.json` |
| `28_refresh_qfq.py` | 强制刷新沪深主板前复权日线到最新 |
| `34_fetch_raw_fast.py` | 抓不复权日线 → `.data/raw_bars.db`（1500 根，2020-08 起） |
| `36_fetch_raw_deep.py` | **抓更深的不复权日线 → `.data/raw_bars_deep.db`（2200 根，2017 起；30 万回测用）** |

## 03_backtest · 回测与报告

| 脚本 | 作用 |
| --- | --- |
| `24_backtest.py` | 五年决策点回测（召回/精度/漏掉率） |
| `25_tune_anchor.py` | 锚点涨幅阈值扫描（4%~10%） |
| `26_tune_band_vol.py` | 横盘幅度 × 量能二维扫描 |
| `27_holdout.py` | 「持有到被剔除」的最终兑现率 |
| `29_export_picks.py` | 导出候选到 CSV / Excel（含止损、目标位、盈亏比） |
| `31_report.py` | 生成 HTML 交易报告 |
| `35_sim_raw.py` | **30 万本金组合回测（真实不复权价格）** |

## 04_trend · 趋势票（涨不停不涨停）

| 脚本 | 作用 |
| --- | --- |
| `10_mine_trend.py` | 挖趋势样本 |
| `11_analyze_trend.py` | 分析教科书条件为何无效 |
| `12_tune_trend.py` | 定向规则搜索 |
| `13_similarity.py` | 模板相似度搜索 |
| `14_build_template.py` | 生成模板向量与标定表 |

## 05_lowpos · 低位票（低位均线粘合）

| 脚本 | 作用 |
| --- | --- |
| `28_lowpos_study.py` | 旧版拐头阈值标定（q 复权，2020-2026） |
| `29_lowpos_consistency.py` | 生产/研究特征一致性校验 |
| `30_lowpos_breakout.py` | **拐头 vs 突破全样本统计（不复权，2021-2026）；生成信号表** |
| `31_lowpos_portfolio.py` | **30 万组合回测（最多 5 只、次日开盘、HTML 报告）** |
| `32_signal_alpha.py` | 每日前 5 个信号的排序 alpha 检验 |
| `33_feature_alpha.py` | 信号特征 → 未来 20 日收益的分层分析 |
| `34_early_alpha.py` | 拐头模式内部排序 / 价格过滤测试 |
| `35_build_dataset.py` | **信号 + 市值/成交额/行业/大盘环境数据集** |
| `36_optimize_filters.py` | **训练-验证筛选/排序分析（小市值/低成交额/大盘过滤）** |

## _archive · 已归档

被取代的实验脚本（user_variant 分支、慢速抓取器、旧版回测等），保留供追溯，不参与运行。
