"""二次涨停形态（美人肩 / 双响炮）—— 特征提取与判定。

结构定义
--------
::

    首板 i (收盘涨停, 收盘价 L)
      ↓  整理 d-i 个交易日：股价围绕 L 小幅震荡
    决策点 d  ← 我们在这里判断「明天会不会二次涨停」
      ↓
    二次涨停 j = d+1

本模块只负责「把形态量化成特征向量」，阈值部分由 :class:`SecondBoardConfig` 提供。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .indicators import (
    board_limit_ratio,
    is_limit_up_bar,
    is_st,
    macd,
    mean,
    moving_average,
    rolling_max_fast,
    slope,
    stdev,
)

# 特征字段（顺序固定，便于导出 CSV / 训练）
FEATURE_KEYS: Tuple[str, ...] = (
    # --- 结构 ---
    "gap",              # 整理天数
    "dev_close_last",   # 决策点收盘相对首板收盘的偏离
    "dev_close_max",    # 整理期收盘最大偏离
    "dev_close_min",    # 整理期收盘最小偏离
    "dev_high_max",     # 整理期最高价最大偏离
    "dev_low_min",      # 整理期最低价最小偏离
    "range_pct",        # 整理期总振幅 (max_high-min_low)/L
    "close_pos",        # 决策点收盘在整理区间中的位置 0~1
    "avg_amp",          # 整理期平均单日振幅
    "max_amp",          # 整理期最大单日振幅
    "up_days",          # 整理期收阳天数占比
    "platform_drift",   # 平台重心漂移（后半段均价-前半段均价）/锚点价
    # --- 量能 ---
    "vol_ratio",        # 整理期均量 / 首板量
    "vol_last_ratio",   # 决策点量 / 首板量
    "vol_trend",        # 后 2 日均量 / 前 2 日均量
    "board_vol_ratio",  # 首板量 / 首板前 5 日均量（放量程度）
    # --- 均线 ---
    "ma_bull",          # MA5>MA10>MA20
    "ma_spread",        # 三线粘合度 (max-min)/mean
    "close_vs_ma5",
    "close_vs_ma10",
    "close_vs_ma20",
    "ma20_slope",       # MA20 近 3 日变化率
    # --- MACD ---
    "dif",
    "dea",
    "hist",
    "dif_n",            # DIF / 价格（剔除股价高低的影响）
    "dea_n",
    "hist_n",
    "dif_pos",          # DIF > 0
    "dea_pos",          # DEA > 0
    "hist_pos",         # MACD 柱 > 0
    "golden_cross",     # 决策点当天 DIF 上穿 DEA
    "hist_up",          # MACD 柱较前一日放大
    "dif_up",           # DIF 较前一日抬升
    # --- 动量 / 位置 ---
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "ret_pre_board",    # 首板前 20 日涨幅
    "dist_ma20",        # 决策点收盘偏离 MA20
    "close_vs_hh60",    # 收盘 / 60 日最高价
    "vol_vs_pre",       # 整理期均量 / 首板前 10 日均量
    "boards_60d",       # 决策点前 60 日涨停次数
    "anchor_gain",      # 锚点日涨幅 %
    "anchor_is_lu",     # 锚点日是否涨停
    "lu_in_window",     # 整理期内是否已再次涨停（1 = 已启动，应排除）
    "price",
    # --- 换手率 / 量能细节（2026-09 新增，用于区分真假美人肩）---
    "turnover_last",     # 决策日换手率 %
    "turnover_cons",     # 整理期平均换手率 %
    "turnover_board",    # 首板换手率 %
    "vol_ratio_5d",      # 决策日量 / 前 5 日均量（量比）
    "vol_ratio_10d",     # 决策日量 / 前 10 日均量
    "vol_peak_ratio",    # 整理期最大单日量 / 首板量
    "vol_peak_pos",      # 天量在整理期的位置（0=第一天，1=最后一天）
    "vol_decay",         # 整理期后1/3均量 / 前1/3均量（越小越缩）
    "hist_decline",      # MACD柱自整理期最高点的回落幅度
    "hist_decline_days", # MACD柱连续衰减天数
    "amp_last",          # 决策日振幅 %
    "upper_shadow_last", # 决策日上影线 / 当日振幅
    "close_pos_day",     # 决策日收盘在当日区间的位置 0~1
    "bear_ratio",        # 整理期阴线（收<开）占比
    "big_loss_days",     # 整理期单日跌幅 > 5% 的天数
    "amount_peak_ratio", # 整理期最大成交额 / 首板成交额
    # --- 筹码 / 市值 / 周K（2026-09 新增）---
    "chip_pct60",        # 收盘在 60 日成本区间中的分位（获利盘代理）
    "cost_dev60",        # 收盘相对 60 日加权均成本的偏离
    "cost_std60",        # 60 日成本离散度（越小筹码越集中）
    "chip_pct120",
    "cost_dev120",
    "float_mv_yi",       # 流通市值（亿元）
    "ln_float_mv",
    "turnover60",        # 60 日均换手率 %（估算）
    "week_ma_bull",      # 周线 MA5>MA10>MA20
    "week_hist_n",       # 周线 MACD 柱 / 价格
    "week_ret_4w",       # 近 4 周涨幅
    # --- 大盘 / 板块上下文（2026-09 新增）---
    "mkt_ret_5d", "mkt_ret_20d", "mkt_dev_ma20", "mkt_hist_n",
    "mkt_week_hist", "mkt_vol_ratio", "mkt_breadth",
    "sec_ret_5d", "sec_ret_20d", "sec_dev_ma20", "sec_hist_n", "sec_rank_5d",
)

# 标签 / 附加字段
META_KEYS: Tuple[str, ...] = (
    "code", "name", "board_date", "decision_date", "next_date",
    "limit_price", "label_next", "label_3d", "fwd_ret",
    # 前视收益（仅用于研究评估，不参与特征）
    "fwd_gap", "fwd_ret_oc", "fwd_ret_2d", "fwd_ret_3d", "fwd_ret_5d",
    "fwd_max_3d", "fwd_min_3d",
)


@dataclass
class MineConfig:
    """样本挖掘的宽松结构过滤（用于构造研究样本集）。"""

    anchor_min_gain: float = 3.0    # 锚点日最小涨幅 %（3 = 涨 3 个点即可当锚点）
    anchor_max_gain: float = 21.0   # 上限（超过说明已经启动，不算"横盘起点"）
    min_gap: int = 2
    max_gap: int = 12
    max_abs_dev: float = 0.10   # 整理期收盘价相对首板价的最大偏离（宽松）
    horizon: int = 1            # 预测未来几天内涨停
    min_history: int = 60


@dataclass
class SecondBoardConfig:
    """二次涨停形态判定阈值（由 31 万样本统计得出，见 research/03-06）。

    参数来源：2019-2026 年全市场 55 万决策点样本，时间外样本验证。
    """

    # --- 锚点：只做涨停/准涨停（第一次涨停 -> 横盘 -> 第二次涨停）---
    anchor_min_gain: float = 9.4   # 锚点日最小涨幅 %（≥9.4% 视为涨停/准涨停）
    anchor_max_gain: float = 10.5  # 锚点日最大涨幅 %（9.5%~10%，含涨停；10.5 为四舍五入余量）
    require_anchor_limit_up: bool = False   # True = 退回"必须涨停"的旧口径

    # --- 横盘整理 ---
    min_gap: int = 4               # 锚点后至少横盘几天（2021-2026回测最优 4~8）
    max_gap: int = 8               # 最多横盘几天
    # 横盘幅度：整理期每天收盘价必须在锚点收盘价的 ±close_band 内
    close_band: float = 0.035      # 0.035 = ±3.5%（回测最优：窄平台 + 量能过滤）
    # 以下为灾难性过滤（防止极端情形），不参与“窄幅”定义
    dev_high_max: float = 0.15     # 整理期最高价相对首板收盘价的上偏上限
    dev_low_min: float = -0.15     # 下偏下限
    dev_close_min: float = -0.12   # 整理期收盘价下偏下限
    dev_close_max: float = 0.12    # 收盘价上偏上限

    # --- 性能：只取尾部多少根参与指标计算 ---
    indicator_bars: int = 200

    # --- 活跃度（最强单一因子） ---
    # 广义口径下只需 ≥1（锚点本身可能不是涨停）；分数会给更高的涨停次数加权
    min_boards_60d: int = 0        # 60 日内涨停次数（0=不限制，交给打分排序）

    # --- 横盘区间 ---
    max_range_pct: float = 0.15    # 整理期总振幅上限（含日内影线）
    min_close_pos: float = 0.0     # 关闭（简单口径）

    # --- 量能（回测：量能趋势 ≥0.8 是精度最强杠杆，7.8%→10.4%）---
    min_vol_ratio: float = 0.0     # 关闭
    max_vol_ratio: float = 999.0   # 关闭
    min_vol_trend: float = 0.9     # 后2日均量/前2日均量 ≥0.9（量能撑住，回测最优）
    min_vol_last_ratio: float = 0.0  # 关闭

    # --- 价格 / 流动性（默认不做过滤）---
    min_price: float = 0.0
    max_price: float = 99999.0

    # --- 波动 ---
    min_avg_amp: float = 0.0       # 整理期平均日振幅下限（0=关闭；温和锚点横盘本就安静）
    # 爬山形态：休息时「日K只是小波动」—— 平均日振幅上限（0.06 = 6%；9.99 = 关闭）
    max_avg_amp: float = 0.04
    # 爬山形态：休息平台的「重心不能向下走」—— 后半段均价相对前半段的漂移下限
    # -9.99 = 关闭；-0.02 表示平台重心下移不得超过锚点价的 2%
    min_platform_drift: float = -0.01

    # --- MACD（已按价格归一化；不在硬过滤里，只参与打分） ---
    min_hist_n: float = -9.99

    # --- 其他 ---
    min_score: float = 0.0         # 综合打分门槛（0~1）
    exclude_st: bool = True
    min_bars: int = 80

    # --- 失败规避（2026-09 新增）---
    # 基于 29 万样本对比「二次涨停成功 vs 失败」得出，
    # 详见 research/16_fail_reasons.py / 17_avoid_fail.py。
    # 实测：形状池命中率 4.94% → 7.56%（1.53x）；
    #       完整策略池 7.06% → 8.26%，并且能避开阔6 只典型失败案例。
    avoid_fail: bool = False   # 大道至简：默认关闭六条失败规避
    max_hist_decline: float = 0.30      # MACD 柱自整理期最高点的回落幅度上限
    # 温和锚点（anchor 不是涨停）分支的专属阈值 —— 由 research/19_mild_anchor.py 标定
    mild_max_hist_decline: float = 0.35     # 温和分支对 MACD 柱回落更宽容
    mild_min_vol_trend: float = 0.60        # 温和分支量能趋势下限

    # --- 深层过滤（2026-09，基于 22.2 万锚点事件 / 6.7 年 1289 只深交所股票）---
    # 实测：形态基准 34.4% → 加满后 54.9%（2.74x），3日回撤从 -3.85% 降到 -2.7%
    min_week_ret_4w: float = 0.0    # 近 4 周涨幅下限 %（5 = 最强单一新特征，34%→49%）
    require_week_hist_pos: bool = False   # 周线 MACD 柱 > 0（39.6%，次日 +3.25%）
    min_turnover60: float = 0.0     # 60 日均换手率下限 %（2 = 37.3%，活跃度代理）
    min_chip_pct60: float = 0.0     # 获利盘分位下限（0.5 → 40.3%）
    exclude_fired: bool = True      # 排除「整理期内已经涨停」的（二次涨停已发生）

    # --- 大盘 / 板块环境（2026-09，基于 22.2 万事件）---
    # 实测：形态基准 34.4%/次日+2.3%/回撤-3.9% → 加环境后 47.2%/+6.1%/-1.4%
    min_sec_ret_5d: float = 0.0     # 板块 5 日涨幅下限 %（最强环境因子 34%→41%）
    min_mkt_ret_5d: float = 0.0     # 大盘 5 日涨幅下限 %
    min_mkt_breadth: float = 0.0    # 全市场上涨家数占比下限（0.5 = 一半以上在涨）
    max_hist_decline_days: int = 2      # MACD 柱连续衰减天数上限
    max_board_vol_ratio: float = 2.20   # 首板放量倍数上限（首板量/首板前5日均量）
    min_vol_decay: float = 0.50         # 整理期后1/3均量 / 前1/3均量 下限
    min_vol_ratio_5d: float = 0.70      # 决策日量 / 前5日均量 下限
    min_amp_last: float = 4.0           # 决策日振幅下限 %

# ============================================================================
# 打分模型
# 权重 = 候选池内 2|AUC-0.5| 归一化；锚点 = 池内 负样本 p50 → 正样本 p75
# （由 research/08_tune.py 在 29 万样本、时间外验证下得出）
# ============================================================================

#: ``(特征, 锚点下, 锚点上, 权重)`` —— 特征值 <=锚点下 得 0 分，>=锚点上 得 1 分
SCORE_SPEC: Tuple[Tuple[str, float, float, float], ...] = (
    ("boards_60d", 3.0, 10.0, 0.112),
    ("price", 12.79, 9.14, 0.087),
    ("ma_spread", 0.060, 0.124, 0.086),
    ("avg_amp", 0.054, 0.081, 0.082),
    ("ret_pre_board", 0.020, 0.192, 0.081),
    ("range_pct", 0.120, 0.159, 0.075),
    ("vol_last_ratio", 0.697, 1.408, 0.071),
    ("dif_n", 0.023, 0.052, 0.070),
    ("ma20_slope", 0.015, 0.036, 0.070),
    ("dev_high_max", 0.068, 0.101, 0.068),
    ("max_amp", 0.084, 0.115, 0.067),
    ("ret_20d", 0.098, 0.256, 0.067),
    ("dist_ma20", 0.051, 0.130, 0.064),
)

#: 打分 → 次日二次涨停概率标定表（由 research/07_calibrate.py 生成）
#: 口径：深交所 + 收盘震幅 ±3% + 整理 3~10 天 + 60日涨停次数 ≥5
#: 2025-01 ~ 2026-09 时间外样本，基准命中率 4.6%
SCORE_CALIBRATION: Tuple[Tuple[float, float], ...] = (
    (0.00, 0.0660),
    (0.40, 0.1060),
    (0.52, 0.1100),
    (0.58, 0.1250),
    (0.65, 0.1350),
    (0.75, 0.1450),
    (0.85, 0.1550),
)


def compute_score(feats: Dict[str, Any]) -> float:
    """把特征字典映射成 0~1 的综合打分。"""
    total = 0.0
    weight = 0.0
    for key, lo, hi, w in SCORE_SPEC:
        v = feats.get(key)
        weight += w
        if v is None:
            total += 0.5 * w
            continue
        v = float(v)
        if hi == lo:
            total += 0.5 * w
            continue
        frac = (v - lo) / (hi - lo)
        total += max(0.0, min(1.0, frac)) * w
    return round(total / weight, 4) if weight else 0.0


def score_to_probability(score: float) -> float:
    """把打分映射成「次日二次涨停」的历史概率（标定表线性插值）。"""
    table = SCORE_CALIBRATION
    if score <= table[0][0]:
        return table[0][1]
    if score >= table[-1][0]:
        return table[-1][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x0 <= score <= x1:
            t = (score - x0) / (x1 - x0)
            return round(y0 + t * (y1 - y0), 4)
    return table[-1][1]


# 人话解释用的字段名
FEATURE_LABEL = {
    "gap": "整理天数",
    "boards_60d": "60日涨停次数",
    "vol_ratio": "整理期量比",
    "vol_trend": "量能趋势",
    "vol_last_ratio": "决策日量比",
    "price": "股价",
    "avg_amp": "平均振幅",
    "hist_n": "MACD柱(归一)",
    "dev_close_last": "收盘偏离首板价",
    "dev_high_max": "最高价偏离",
    "dev_low_min": "最低价偏离",
    "ma_spread": "均线发散度",
}


def structure_reasons(feats: Dict[str, Any], cfg: SecondBoardConfig) -> List[str]:
    """返回不满足的结构条件（空列表 = 全部通过）。"""
    bad: List[str] = []
    checks = (
        ("gap", lambda v: cfg.min_gap <= v <= cfg.max_gap, f"整理天数需在 {cfg.min_gap}~{cfg.max_gap} 天"),
        ("boards_60d", lambda v: v >= cfg.min_boards_60d, f"60日涨停次数需 ≥ {cfg.min_boards_60d}"),
        ("dev_high_max", lambda v: v <= cfg.dev_high_max, f"最高价上偏需 ≤ {cfg.dev_high_max:.1%}"),
        ("dev_low_min", lambda v: v >= cfg.dev_low_min, f"最低价下偏需 ≥ {cfg.dev_low_min:.1%}"),
        ("dev_close_min", lambda v: v >= cfg.dev_close_min, f"收盘下偏需 ≥ {cfg.dev_close_min:.1%}"),
        ("dev_close_max", lambda v: v <= cfg.dev_close_max, f"收盘上偏需 ≤ {cfg.dev_close_max:.1%}"),
        ("vol_ratio", lambda v: cfg.min_vol_ratio <= v <= cfg.max_vol_ratio,
         f"整理期量比需在 {cfg.min_vol_ratio}~{cfg.max_vol_ratio}"),
        ("vol_trend", lambda v: v >= cfg.min_vol_trend, f"量能趋势需 ≥ {cfg.min_vol_trend}"),
        ("vol_last_ratio", lambda v: v >= cfg.min_vol_last_ratio, f"决策日量比需 ≥ {cfg.min_vol_last_ratio}"),
        ("price", lambda v: cfg.min_price <= v <= cfg.max_price, f"股价需在 {cfg.min_price}~{cfg.max_price} 元"),
        ("avg_amp", lambda v: v >= cfg.min_avg_amp, f"平均振幅需 ≥ {cfg.min_avg_amp:.1%}"),
        ("hist_n", lambda v: v >= cfg.min_hist_n, f"MACD柱(归一)需 ≥ {cfg.min_hist_n}"),
    )
    if cfg.max_range_pct > 0:
        checks += (
            ("range_pct", lambda v: v <= cfg.max_range_pct,
             f"整理期总振幅需 ≤ {cfg.max_range_pct:.0%}"),
        )
    # ---- 爬山形态过滤 ----
    if cfg.max_avg_amp < 1.0:
        checks += (
            ("avg_amp", lambda v: v <= cfg.max_avg_amp,
             f"整理期平均日振幅需 ≤ {cfg.max_avg_amp:.1%}（休息时只是小波动）"),
        )
    if cfg.min_platform_drift > -9.0:
        checks += (
            ("platform_drift", lambda v: v >= cfg.min_platform_drift,
             f"平台重心不得下移超过 {-cfg.min_platform_drift:.1%}（休息平台不能向下）"),
        )
    if cfg.min_close_pos > 0:
        checks += (
            ("close_pos", lambda v: v >= cfg.min_close_pos,
             f"决策日收盘位置需 ≥ {cfg.min_close_pos}"),
        )
    # ---- 深层过滤（周K / 换手 / 筹码）----
    if cfg.min_week_ret_4w:
        checks += (("week_ret_4w", lambda v: v >= cfg.min_week_ret_4w,
                    f"近4周涨幅需 ≥ {cfg.min_week_ret_4w}%"),)
    if cfg.require_week_hist_pos:
        checks += (("week_hist_n", lambda v: v > 0, "周线MACD柱需 > 0（周线走强）"),)
    if cfg.min_turnover60:
        checks += (("turnover60", lambda v: v >= cfg.min_turnover60,
                    f"60日均换手率需 ≥ {cfg.min_turnover60}%"),)
    if cfg.min_chip_pct60:
        checks += (("chip_pct60", lambda v: v >= cfg.min_chip_pct60,
                    f"获利盘分位需 ≥ {cfg.min_chip_pct60}"),)
    if cfg.exclude_fired:
        checks += (("lu_in_window", lambda v: v == 0,
                    "整理期内不得已出现涨停（二次涨停已发生）"),)
    # ---- 大盘 / 板块环境（数据缺失时跳过，不误杀）----
    ctx_ok = feats.get("ctx_ok")
    has_ctx = (ctx_ok is None) or (float(ctx_ok) > 0)
    if cfg.min_sec_ret_5d and has_ctx:
        checks += (("sec_ret_5d", lambda v: v >= cfg.min_sec_ret_5d,
                    f"板块5日涨幅需 ≥ {cfg.min_sec_ret_5d}%（板块不能是逆风）"),)
    if cfg.min_mkt_ret_5d and has_ctx:
        checks += (("mkt_ret_5d", lambda v: v >= cfg.min_mkt_ret_5d,
                    f"大盘5日涨幅需 ≥ {cfg.min_mkt_ret_5d}%"),)
    if cfg.min_mkt_breadth and has_ctx:
        checks += (("mkt_breadth", lambda v: v >= cfg.min_mkt_breadth,
                    f"上涨家数占比需 ≥ {cfg.min_mkt_breadth:.0%}"),)
    for key, ok, msg in checks:
        v = feats.get(key)
        if v is None or not ok(float(v)):
            bad.append(msg)
    if cfg.close_band > 0:
        lo, hi = feats.get("dev_close_min"), feats.get("dev_close_max")
        if lo is None or float(lo) < -cfg.close_band:
            bad.append(f"整理期收盘下偏需 ≥ {-cfg.close_band:.1%}")
        if hi is None or float(hi) > cfg.close_band:
            bad.append(f"整理期收盘上偏需 ≤ {cfg.close_band:.1%}")

    # ---- 失败规避（按锚点类型分支，两套规则各自标定）----
    def _chk(key: str, op: str, t: float, msg: str) -> None:
        v = feats.get(key)
        if v is None:
            bad.append(msg)
            return
        fv = float(v)
        if (op == "<=" and fv > t) or (op == ">=" and fv < t):
            bad.append(msg)

    if cfg.avoid_fail:
        is_lu = bool(feats.get("anchor_is_lu"))
        if is_lu:
            # 涨停锚点分支：六条规则（基于 44,326 个涨停美人肩事件标定）
            _chk("hist_decline", "<=", cfg.max_hist_decline,
                 f"MACD柱自高点回落需 ≤ {cfg.max_hist_decline:.0%}")
            _chk("hist_decline_days", "<=", cfg.max_hist_decline_days,
                 f"MACD柱连衰需 ≤ {cfg.max_hist_decline_days} 天")
            _chk("board_vol_ratio", "<=", cfg.max_board_vol_ratio,
                 f"首板放量需 ≤ {cfg.max_board_vol_ratio:.1f} 倍（巨量首板多为出货）")
            _chk("vol_decay", ">=", cfg.min_vol_decay,
                 f"整理期后1/3均量/前1/3均量需 ≥ {cfg.min_vol_decay}")
            _chk("vol_ratio_5d", ">=", cfg.min_vol_ratio_5d,
                 f"决策日量/前5日均量需 ≥ {cfg.min_vol_ratio_5d}")
            _chk("amp_last", ">=", cfg.min_amp_last,
                 f"决策日振幅需 ≥ {cfg.min_amp_last:.1f}%")
        else:
            # 温和锚点分支（涨 3~9.5% 后横盘）：只有 MACD 柱衰减与量能两条有区分度
            _chk("hist_decline", "<=", cfg.mild_max_hist_decline,
                 f"MACD柱自高点回落需 ≤ {cfg.mild_max_hist_decline:.0%}")
            _chk("hist_decline_days", "<=", cfg.max_hist_decline_days,
                 f"MACD柱连衰需 ≤ {cfg.max_hist_decline_days} 天")
            _chk("vol_trend", ">=", cfg.mild_min_vol_trend,
                 f"量能趋势需 ≥ {cfg.mild_min_vol_trend}")
    return bad


def detect_second_board(
    bars: Sequence[Dict[str, Any]],
    code: str,
    name: str = "",
    cfg: Optional[SecondBoardConfig] = None,
    quote: Optional[Dict[str, Any]] = None,
    collect: bool = False,
    float_shares: float = 0.0,
    mctx: Any = None,
):
    """实盘判定：以**最后一根 K 线为决策点**，找是否存在符合条件的二次涨停形态。

    ``collect=False``（默认）返回打分最高的一个匹配（或 ``None``）；
    ``collect=True`` 返回**所有**首板候选（每个都带 ``features``，不做结构过滤），
    供调用方用不同阈值反复筛选，避免重扫。

    性能设计：先用 ``max_gap`` 次简单比较做预筛，只有存在首板的股票才会构建
    MA/MACD 等指标；且指标只基于尾部 ``indicator_bars`` 根计算。
    """
    cfg = cfg or SecondBoardConfig()
    if cfg.exclude_st and is_st(name):
        return [] if collect else None
    n = len(bars)
    if n < cfg.min_bars:
        return [] if collect else None

    ratio = board_limit_ratio(code, name)
    d = n - 1

    # ---- 快速预筛：最近 max_gap 根内是否存在"锚点日" ----
    def _is_anchor(i: int) -> bool:
        pc = bars[i - 1].get("close") if i >= 1 else None
        cl = bars[i].get("close")
        if not pc or cl is None:
            return False
        if cfg.require_anchor_limit_up:
            return is_limit_up_bar(bars[i], pc, ratio)
        return cfg.anchor_min_gain <= (cl - pc) / pc * 100.0 < cfg.anchor_max_gain

    cand_idx = [
        i
        for i in range(max(1, d - cfg.max_gap), d - cfg.min_gap + 1)
        if _is_anchor(i)
    ]
    if not cand_idx:
        return [] if collect else None

    # ---- 只对候选股构建指标，且只看尾部 ----
    keep = min(n, max(cfg.indicator_bars, cfg.max_gap + 70))
    offset = n - keep
    sub = list(bars[offset:]) if offset else list(bars)
    ctx = StockContext(sub, code, name, float_shares)
    d2 = ctx.n - 1

    found: List[Dict[str, Any]] = []
    for i in cand_idx:
        i2 = i - offset
        if i2 < 26:
            continue
        feats = extract_setup(ctx, i2, d2, mctx)
        if feats is None:
            continue
        score = compute_score(feats)
        if not collect:
            if structure_reasons(feats, cfg):
                continue
            if score < cfg.min_score:
                continue

        prob = score_to_probability(score)
        rec: Dict[str, Any] = {
            "strategy": "second_board",
            "code": code,
            "name": name,
            "score": round(score * 100, 1),
            "score_raw": score,
            "prob": prob,
            "首板日": bars[i].get("date"),
            "首板价": round(ctx.closes[i2], 2),
            "锚点涨幅%": round(feats["anchor_gain"], 2),
            "锚点类型": "涨停" if feats["anchor_is_lu"] else "温和阳线",
            "决策日": bars[d].get("date"),
            "现价": round(ctx.closes[d2], 2),
            "整理天数": feats["gap"],
            "60日涨停次数": int(feats["boards_60d"]),
            "距首板价%": round(feats["dev_close_last"] * 100, 2),
            "整理期最高偏离%": round(feats["dev_high_max"] * 100, 2),
            "整理期最低偏离%": round(feats["dev_low_min"] * 100, 2),
            "收盘下偏%": round(feats["dev_close_min"] * 100, 2),
            "收盘上偏%": round(feats["dev_close_max"] * 100, 2),
            "整理期量比": round(feats["vol_ratio"], 2),
            "量能趋势": round(feats["vol_trend"], 2),
            "决策日量比": round(feats["vol_last_ratio"], 2),
            "首板放量倍数": round(feats["board_vol_ratio"], 2),
            "平均振幅%": round(feats["avg_amp"] * 100, 2),
            "MA5": ctx.ma5[d2],
            "MA10": ctx.ma10[d2],
            "MA20": ctx.ma20[d2],
            "均线多头": bool(feats["ma_bull"]),
            "均线发散度": round(feats["ma_spread"], 4),
            "MACD_DIF": round(feats["dif"], 4) if feats["dif"] is not None else None,
            "MACD_DEA": round(feats["dea"], 4) if feats["dea"] is not None else None,
            "MACD柱": round(feats["hist"], 4) if feats["hist"] is not None else None,
            "MACD柱归一": round(feats["hist_n"], 5),
            "MACD金叉": bool(feats["golden_cross"]),
            "收盘位置": round(feats["close_pos"], 2),
            "range_pct": feats["range_pct"],
            "近5日涨幅%": round((feats["ret_5d"] or 0) * 100, 2),
            "首板前20日涨幅%": round((feats["ret_pre_board"] or 0) * 100, 2),
        }
        if quote:
            rec.update({
                "现价": quote.get("price") or rec["现价"],
                "今日涨跌%": quote.get("change_pct"),
                "成交额": quote.get("amount"),
                "换手率%": quote.get("turnover_rate"),
                "总市值": quote.get("total_mv"),
                "行业": quote.get("industry"),
            })
        if collect:
            rec["features"] = feats
            found.append(rec)
        elif not found or rec["score_raw"] > found[0]["score_raw"]:
            found = [rec]
    if collect:
        return found
    return found[0] if found else None


# ---------------------------------------------------------------- 预计算


_WEEK_KEY_CACHE: Dict[str, Any] = {}


def _date_week_key(dt: str) -> Any:
    """日期字符串 → (年, ISO周)，带缓存。

    同一日期在同一次全市场扫描中会被反复解析（每只股票每根 K 线一次），
    缓存后 ``strptime`` 的调用次数从 8 万+ 降到几千。
    """
    key = _WEEK_KEY_CACHE.get(dt)
    if key is None:
        try:
            key = (dt[:4], datetime.strptime(dt, "%Y-%m-%d").isocalendar()[1])
        except Exception:  # noqa: BLE001
            key = dt[:7]
        if len(_WEEK_KEY_CACHE) < 40000:
            _WEEK_KEY_CACHE[dt] = key
    return key


class StockContext:
    """单只股票的预计算指标，避免重复遍历。"""
    __slots__ = (
        "bars", "code", "name", "n", "ratio", "lu", "closes", "vols",
        "highs", "lows", "opens", "amounts",
        "ma5", "ma10", "ma20", "ma30", "ma60", "dif", "dea", "hist",
        "hh20", "hh60", "hh120", "float_shares", "chg",
        "cost", "csum", "csum2", "cw", "cost_min60", "cost_max60", "week",
        "_wk_close", "_wk_end",
    )

    def __init__(
        self, bars: Sequence[Dict[str, Any]], code: str, name: str = "",
        float_shares: float = 0.0,
    ) -> None:
        self.bars = bars
        self.code = code
        self.name = name
        self.float_shares = float(float_shares or 0)
        self.n = len(bars)
        self.ratio = board_limit_ratio(code, name)
        self.closes = [b.get("close") for b in bars]
        self.vols = [float(b.get("volume") or 0) for b in bars]
        self.highs = [b.get("high") for b in bars]
        self.lows = [b.get("low") for b in bars]
        self.opens = [b.get("open") for b in bars]
        self.amounts = [float(b.get("amount") or 0) for b in bars]
        self.lu: List[bool] = [False] * self.n
        self.chg: List[Optional[float]] = [None] * self.n
        for i in range(1, self.n):
            pc = self.closes[i - 1]
            if pc and self.closes[i] is not None:
                self.chg[i] = (self.closes[i] - pc) / pc * 100.0
            self.lu[i] = is_limit_up_bar(bars[i], self.closes[i - 1], self.ratio)
        self.ma5 = moving_average(self.closes, 5)
        self.ma10 = moving_average(self.closes, 10)
        self.ma20 = moving_average(self.closes, 20)
        self.ma30 = moving_average(self.closes, 30)
        self.ma60 = moving_average(self.closes, 60)
        self.dif, self.dea, self.hist = macd(self.closes)
        # 滚动最高价（预计算，形态判定时 O(1) 取用）
        self.hh20 = rolling_max_fast(self.highs, 20)
        self.hh60 = rolling_max_fast(self.highs, 60)
        self.hh120 = rolling_max_fast(self.highs, 120)
        # ---------- 筹码成本（amount/volume 得到当日均价）----------
        self.cost: List[Optional[float]] = []
        for i in range(self.n):
            v, a = self.vols[i], self.amounts[i]
            self.cost.append((a / (v * 100.0)) if (v and a) else None)
        cs: List[float] = [0.0]
        cs2: List[float] = [0.0]
        cw: List[float] = [0.0]
        for i in range(self.n):
            c, v = self.cost[i], self.vols[i]
            if c is None or not v:
                cs.append(cs[-1]); cs2.append(cs2[-1]); cw.append(cw[-1])
            else:
                cs.append(cs[-1] + c * v)
                cs2.append(cs2[-1] + c * c * v)
                cw.append(cw[-1] + v)
        self.csum, self.csum2, self.cw = cs, cs2, cw
        self.cost_min60 = rolling_max_fast(
            [-x if x is not None else None for x in self.cost], 60
        )
        self.cost_max60 = rolling_max_fast(self.cost, 60)
        # ---------- 周 K（按 ISO 周聚合，预计算以做到 O(1) 查询）----------
        self.week = [0] * self.n
        wk = -1
        last_key: Any = None
        for i in range(self.n):
            dt = str(self.bars[i].get("date") or "")
            key = _date_week_key(dt)
            if key != last_key:
                wk += 1
                last_key = key
            self.week[i] = wk
        # 每周最后一根的收盘价 + 该周的结束索引
        self._wk_close: List[float] = []
        self._wk_end: List[int] = []
        for i in range(self.n):
            c = self.closes[i]
            if c is None:
                continue
            if self._wk_close and self._wk_end[-1] == self.week[i]:
                self._wk_close[-1] = c
            else:
                self._wk_close.append(c)
                self._wk_end.append(self.week[i])

    # ---- 筹码成本指标（前缀和，O(1)）----
    def chip_stats(self, d: int, win: int) -> Tuple[float, float, float]:
        """返回 ``(收盘在成本区间中的分位, 相对均成本偏离, 成本离散度)``。"""
        lo = max(0, d - win + 1)
        w = self.cw[d + 1] - self.cw[lo]
        s1 = self.csum[d + 1] - self.csum[lo]
        s2 = self.csum2[d + 1] - self.csum2[lo]
        c = self.closes[d]
        if w <= 0 or not c:
            return 0.5, 0.0, 0.0
        avg = s1 / w
        var = max(s2 / w - avg * avg, 0.0)
        cmin = self.cost_min60[d]
        cmax = self.cost_max60[d]
        cmin = -cmin if cmin is not None else None
        span = (cmax - cmin) if (cmax is not None and cmin is not None) else 0.0
        pct = ((c - cmin) / span) if span and span > 0 else 0.5
        return (max(0.0, min(1.0, pct)),
                ((c - avg) / avg if avg else 0.0),
                ((var ** 0.5) / avg if avg else 0.0))

    def weekly_series(self, d: int, weeks: int = 40) -> List[float]:
        """截止到 ``d`` 的周收盘序列（O(1)：用二分找到周索引后切片）。"""
        import bisect

        hi = bisect.bisect_right(self._wk_end, self.week[d])
        return self._wk_close[max(0, hi - weeks):hi]

    def is_anchor(self, i: int, min_gain: float, max_gain: float) -> bool:
        """锚点日：涨幅在 ``[min_gain, max_gain]`` 之间（含涨停）。"""
        if i < 1 or i >= self.n:
            return False
        c = self.chg[i]
        if c is None:
            return False
        return min_gain <= c < max_gain   # 上界开区间：9.4% 只归经典口径

    def limit_up_count(self, start: int, end: int) -> int:
        return sum(1 for k in range(max(0, start), min(self.n, end)) if self.lu[k])


# ---------------------------------------------------------------- 特征提取


def extract_setup(
    ctx: StockContext, i: int, d: int, mctx: Any = None
) -> Optional[Dict[str, Any]]:
    """提取「首板 i + 决策点 d」的特征。**只使用 bars[:d+1] 的数据，无未来函数。**

    ``mctx`` 为 :class:`~stock_agent.screener.context.MarketContext`，
    用于附加大盘 / 板块环境特征。
    """
    if i < 25 or d <= i or d >= ctx.n:
        return None
    L = ctx.closes[i]
    if not L:
        return None

    cons = range(i + 1, d + 1)          # 整理期（含决策点当天）
    c_hi = [ctx.highs[k] for k in cons if ctx.highs[k] is not None]
    c_lo = [ctx.lows[k] for k in cons if ctx.lows[k] is not None]
    c_cl = [ctx.closes[k] for k in cons if ctx.closes[k] is not None]
    if not c_hi or not c_lo or not c_cl:
        return None

    top, bottom = max(c_hi), min(c_lo)
    span = max(top - bottom, 1e-9)
    close_d = ctx.closes[d]
    hh60 = max(
        [v for v in ctx.highs[max(0, d - 59) : d + 1] if v is not None] or [close_d]
    )

    amps = []
    for k in cons:
        pc = ctx.closes[k - 1]
        if pc and ctx.highs[k] is not None and ctx.lows[k] is not None:
            amps.append((ctx.highs[k] - ctx.lows[k]) / pc)

    # ---- 爬山形态：休息平台的重心是否向下（后半段均价 vs 前半段均价）----
    _half = max(1, len(c_cl) // 2)
    _first_avg = mean(c_cl[:_half]) or L
    _second_avg = mean(c_cl[-_half:]) or L
    platform_drift = (_second_avg - _first_avg) / L

    vols_cons = [ctx.vols[k] for k in cons]
    vol_board = ctx.vols[i]
    pre5 = ctx.vols[max(0, i - 5) : i]
    head2 = ctx.vols[i + 1 : i + 3] or [vol_board]
    tail2 = ctx.vols[max(i + 1, d - 1) : d + 1] or [vol_board]

    ma5, ma10, ma20 = ctx.ma5[d], ctx.ma10[d], ctx.ma20[d]
    ma_vals = [v for v in (ma5, ma10, ma20) if v]
    dif, dea, hist = ctx.dif[d], ctx.dea[d], ctx.hist[d]
    dif_p, dea_p, hist_p = ctx.dif[d - 1], ctx.dea[d - 1], ctx.hist[d - 1]

    def rel(a: Optional[float], b: Optional[float]) -> Optional[float]:
        return (a - b) / b if (a is not None and b) else None

    up_days = sum(
        1 for k in cons if ctx.closes[k] is not None and ctx.opens[k] and ctx.closes[k] >= ctx.opens[k]
    )

    # ---------------- 新增：换手率 / 量能细节 ----------------
    fs = ctx.float_shares

    def turnover(k: int) -> float:
        """换手率 %（用当前流通股本近似；股本变动时会有偏差）。"""
        if not fs:
            return 0.0
        return ctx.vols[k] * 100.0 / fs * 100.0

    cons_list = list(cons)
    n_cons = len(cons_list)
    vols_cons_full = [ctx.vols[k] for k in cons_list]
    prior5 = ctx.vols[max(0, d - 5) : d] or [vol_board or 1.0]
    prior10 = ctx.vols[max(0, d - 10) : d] or prior5
    third = max(1, n_cons // 3)
    head_third = mean(vols_cons_full[:third]) or 1.0
    tail_third = mean(vols_cons_full[-third:])
    peak_vol = max(vols_cons_full) if vols_cons_full else 0.0
    peak_idx = vols_cons_full.index(peak_vol) if vols_cons_full else 0
    peak_pos = (peak_idx / max(n_cons - 1, 1)) if n_cons > 1 else 0.0

    amts_cons = [ctx.amounts[k] for k in cons_list]
    amt_board = ctx.amounts[i] or 0.0

    hist_win = [ctx.hist[k] for k in cons_list if ctx.hist[k] is not None]
    hist_peak = max(hist_win) if hist_win else 0.0
    hist_now = ctx.hist[d] if ctx.hist[d] is not None else 0.0
    hist_decline = ((hist_peak - hist_now) / hist_peak) if hist_peak > 0 else 0.0
    decline_days = 0
    for k in range(d, i, -1):
        a, b2 = ctx.hist[k], ctx.hist[k - 1]
        if a is None or b2 is None:
            break
        if a < b2:
            decline_days += 1
        else:
            break

    amp_last = 0.0
    upper_shadow = 0.0
    close_pos_day = 0.5
    if ctx.highs[d] is not None and ctx.lows[d] is not None and ctx.closes[d - 1]:
        rng = ctx.highs[d] - ctx.lows[d]
        amp_last = rng / ctx.closes[d - 1] * 100.0
        if rng > 0:
            upper_shadow = (ctx.highs[d] - ctx.closes[d]) / rng
            close_pos_day = (ctx.closes[d] - ctx.lows[d]) / rng

    bear = sum(
        1 for k in cons_list if ctx.opens[k] and ctx.closes[k] is not None and ctx.closes[k] < ctx.opens[k]
    )
    big_loss = sum(1 for k in cons_list if ctx.closes[k - 1] and ctx.closes[k] is not None
                   and (ctx.closes[k] - ctx.closes[k - 1]) / ctx.closes[k - 1] < -0.05)

    # ---- 筹码 / 周K ----
    _cp60 = ctx.chip_stats(d, 60)
    _cp120 = ctx.chip_stats(d, 120)
    wclose = ctx.weekly_series(d, 40)
    if len(wclose) >= 20:
        wma5, wma10, wma20 = mean(wclose[-5:]), mean(wclose[-10:]), mean(wclose[-20:])
        _wdif, _wdea, _whist = macd(wclose)
        _wh = _whist[-1] if (_whist and _whist[-1] is not None) else 0.0
        _wk = [
            int(wma5 > wma10 > wma20),
            (_wh / close_d) if close_d else 0.0,
            ((wclose[-1] / wclose[-5] - 1) * 100) if (len(wclose) >= 5 and wclose[-5]) else 0.0,
        ]
    else:
        _wk = [0, 0.0, 0.0]

    out: Dict[str, Any] = {
        # 结构
        "gap": d - i,
        "dev_close_last": rel(close_d, L),
        "dev_close_max": (max(c_cl) - L) / L,
        "dev_close_min": (min(c_cl) - L) / L,
        "dev_high_max": (top - L) / L,
        "dev_low_min": (bottom - L) / L,
        "range_pct": span / L,
        "close_pos": (close_d - bottom) / span,
        "avg_amp": mean(amps) if amps else 0.0,
        "max_amp": max(amps) if amps else 0.0,
        "up_days": up_days / max(1, len(list(cons))),
        "platform_drift": platform_drift,
        # 量能
        "vol_ratio": mean(vols_cons) / vol_board if vol_board else 0.0,
        "vol_last_ratio": ctx.vols[d] / vol_board if vol_board else 0.0,
        "vol_trend": mean(tail2) / mean(head2) if mean(head2) else 0.0,
        "board_vol_ratio": vol_board / mean(pre5) if mean(pre5) else 0.0,
        # 均线
        "ma_bull": int(bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)),
        "ma_spread": ((max(ma_vals) - min(ma_vals)) / mean(ma_vals)) if len(ma_vals) == 3 else 0.0,
        "close_vs_ma5": rel(close_d, ma5),
        "close_vs_ma10": rel(close_d, ma10),
        "close_vs_ma20": rel(close_d, ma20),
        "ma20_slope": slope(ctx.ma20[: d + 1], 3) or 0.0,
        # MACD
        "dif": dif,
        "dea": dea,
        "hist": hist,
        "dif_n": (dif / close_d) if (dif is not None and close_d) else 0.0,
        "dea_n": (dea / close_d) if (dea is not None and close_d) else 0.0,
        "hist_n": (hist / close_d) if (hist is not None and close_d) else 0.0,
        "dif_pos": int(bool(dif and dif > 0)),
        "dea_pos": int(bool(dea and dea > 0)),
        "hist_pos": int(bool(hist and hist > 0)),
        "golden_cross": int(bool(dif_p is not None and dea_p is not None and dif and dea
                                 and dif_p <= dea_p and dif > dea)),
        "hist_up": int(bool(hist_p is not None and hist is not None and hist > hist_p)),
        "dif_up": int(bool(dif_p is not None and dif is not None and dif > dif_p)),
        # 动量
        "ret_5d": rel(close_d, ctx.closes[d - 5]) if d >= 5 else None,
        "ret_10d": rel(close_d, ctx.closes[d - 10]) if d >= 10 else None,
        "ret_20d": rel(close_d, ctx.closes[d - 20]) if d >= 20 else None,
        "ret_pre_board": rel(ctx.closes[i - 1], ctx.closes[i - 21]) if i >= 21 else None,
        "dist_ma20": rel(close_d, ma20),
        "close_vs_hh60": rel(close_d, hh60),
        "vol_vs_pre": (
            mean(vols_cons) / mean(ctx.vols[max(0, i - 10) : i])
            if mean(ctx.vols[max(0, i - 10) : i])
            else 0.0
        ),
        "boards_60d": ctx.limit_up_count(d - 60, d + 1),
        "anchor_gain": ctx.chg[i] if ctx.chg[i] is not None else 0.0,
        "anchor_is_lu": int(ctx.lu[i]),
        "lu_in_window": int(any(ctx.lu[k] for k in range(i + 1, d + 1))),
        "price": close_d,
        # 新增
        "turnover_last": turnover(d),
        "turnover_cons": (mean([turnover(k) for k in cons_list]) if fs else 0.0),
        "turnover_board": turnover(i),
        "vol_ratio_5d": ctx.vols[d] / mean(prior5) if mean(prior5) else 0.0,
        "vol_ratio_10d": ctx.vols[d] / mean(prior10) if mean(prior10) else 0.0,
        "vol_peak_ratio": (peak_vol / vol_board) if vol_board else 0.0,
        "vol_peak_pos": peak_pos,
        "vol_decay": (tail_third / head_third) if head_third else 0.0,
        "hist_decline": hist_decline,
        "hist_decline_days": decline_days,
        "amp_last": amp_last,
        "upper_shadow_last": upper_shadow,
        "close_pos_day": close_pos_day,
        "bear_ratio": bear / max(n_cons, 1),
        "big_loss_days": big_loss,
        "amount_peak_ratio": (max(amts_cons) / amt_board) if amt_board else 0.0,
        # ---- 筹码 / 市值 / 周K ----
        "chip_pct60": _cp60[0],
        "cost_dev60": _cp60[1],
        "cost_std60": _cp60[2],
        "chip_pct120": _cp120[0],
        "cost_dev120": _cp120[1],
        "float_mv_yi": (max(ctx.float_shares * close_d / 1e8, 0.0) if ctx.float_shares else 0.0),
        "ln_float_mv": math.log(max(ctx.float_shares * close_d / 1e8, 1e-6))
        if ctx.float_shares > 0 else 0.0,
        "turnover60": (
            mean([ctx.vols[k] * 100.0 / ctx.float_shares * 100.0
                  for k in range(max(0, d - 59), d + 1)])
            if ctx.float_shares else 0.0
        ),
        "week_ma_bull": _wk[0],
        "week_hist_n": _wk[1],
        "week_ret_4w": _wk[2],
    }
    if mctx is not None:
        dt = ctx.bars[d].get("date")
        if dt:
            out.update(mctx.features(ctx.code, dt))
    return out


# ---------------------------------------------------------------- 事件挖掘


def mine_stock(
    bars: Sequence[Dict[str, Any]],
    code: str,
    name: str,
    cfg: Optional[MineConfig] = None,
    float_shares: float = 0.0,
    mctx: Any = None,
) -> List[Dict[str, Any]]:
    """挖掘单只股票的所有 (首板, 决策点) 样本，带 label。

    对每个首板 i，向前推进决策点 d：
      * 若 d+1 是涨停 → 正样本（label_next=1）
      * 否则为负样本
    整理期偏离超过 ``cfg.max_abs_dev`` 即停止该首板的样本生成。
    """
    cfg = cfg or MineConfig()
    ctx = StockContext(bars, code, name, float_shares)
    if ctx.n < cfg.min_history:
        return []

    out: List[Dict[str, Any]] = []
    for i in range(25, ctx.n - 1):
        if not ctx.is_anchor(i, cfg.anchor_min_gain, cfg.anchor_max_gain):
            continue
        L = ctx.closes[i]
        for d in range(i + cfg.min_gap, min(i + cfg.max_gap + 1, ctx.n - 1)):
            cd = ctx.closes[d]
            if cd is None or abs(cd - L) / L > cfg.max_abs_dev:
                break
            feats = extract_setup(ctx, i, d, mctx)
            if feats is None:
                continue
            nxt = d + 1
            lab_next = int(ctx.lu[nxt])
            lab_3d = int(any(ctx.lu[k] for k in range(nxt, min(nxt + cfg.horizon + 2, ctx.n))))

            def fwd_ref(offset: int) -> Optional[float]:
                k = nxt + offset
                c = ctx.closes[k] if k < ctx.n else None
                return (c / cd - 1.0) if (cd and c) else None

            fwd = fwd_ref(0)
            f3 = nxt + 2
            h3 = [ctx.highs[k] for k in range(nxt, min(nxt + 3, ctx.n)) if ctx.highs[k] is not None]
            l3 = [ctx.lows[k] for k in range(nxt, min(nxt + 3, ctx.n)) if ctx.lows[k] is not None]
            o_nxt = ctx.opens[nxt] if nxt < ctx.n else None
            c_nxt = ctx.closes[nxt] if nxt < ctx.n else None
            rec = {
                "code": code,
                "name": name,
                "board_date": bars[i].get("date"),
                "decision_date": bars[d].get("date"),
                "next_date": bars[nxt].get("date"),
                "limit_price": L,
                "label_next": lab_next,
                "label_3d": lab_3d,
                "fwd_ret": fwd,
                "fwd_gap": ((o_nxt / cd - 1.0) if (cd and o_nxt) else None),
                "fwd_ret_oc": ((c_nxt / o_nxt - 1.0) if (o_nxt and c_nxt) else None),
                "fwd_ret_2d": fwd_ref(1),
                "fwd_ret_3d": fwd_ref(2),
                "fwd_ret_5d": fwd_ref(4),
                "fwd_max_3d": ((max(h3) / cd - 1.0) if (cd and h3) else None),
                "fwd_min_3d": ((min(l3) / cd - 1.0) if (cd and l3) else None),
                **feats,
            }
            out.append(rec)
            if lab_next:
                break   # 已命中，同一次首板不再产生更多样本
    return out


def count_events(samples: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """按「首板事件」去重统计正样本数量。"""
    pos = {(s["code"], s["board_date"]) for s in samples if s.get("label_next")}
    all_ev = {(s["code"], s["board_date"]) for s in samples}
    return {
        "samples": len(samples),
        "positives": sum(1 for s in samples if s.get("label_next")),
        "events": len(all_ev),
        "positive_events": len(pos),
    }
