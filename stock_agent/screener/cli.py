"""选股器命令行。

用法::

    python -m stock_agent.screener meirenjian              # 找美人肩
    python -m stock_agent.screener trend                   # 找趋势票（涨不停不涨停）
    python -m stock_agent.screener lowpos                  # 找低位票（均线粘合拐头）
    python -m stock_agent.screener warmup                  # 预热全市场日线缓存
    python -m stock_agent.screener stats                   # 查看本地缓存
    python -m stock_agent.screener meirenjian --help
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import unicodedata
from typing import Any, Dict, List, Optional

from .patterns import SCORE_SPEC, SecondBoardConfig
from .trend import SteadyTrendConfig, TREND_BASELINE
from .lowpos import LowPosConfig
from .scanner import ConsoleProgress, Screener
from .strategies import MeiRenJianConfig, SteadyClimbConfig
from .store import ScreenerStore

# ---------------------------------------------------------------- 终端着色


def enable_ansi() -> bool:
    """Windows 10+ 控制台默认关闭 ANSI，这里手动打开。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


class C:
    enabled = True
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"

    @classmethod
    def paint(cls, text: Any, color: str) -> str:
        if not cls.enabled or not color:
            return str(text)
        return f"{color}{text}{cls.RESET}"


# ---------------------------------------------------------------- 表格工具


def _width(text: Any) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def _pad(text: Any, width: int, align: str = "left") -> str:
    gap = max(0, width - _width(text))
    return (" " * gap + str(text)) if align == "right" else (str(text) + " " * gap)


def render_table(
    headers: List[str],
    aligns: List[str],
    rows: List[List[str]],
    title: str = "",
    colors: Optional[List[Optional[str]]] = None,
) -> str:
    """渲染带中英混排对齐的表格。``colors`` 为每列固定色。"""
    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(cell))

    lines: List[str] = []
    if title:
        lines.append(C.paint(f"  {title}", C.BOLD + C.CYAN))
    sep = "  " + C.paint("─" * (sum(widths) + 2 * len(widths)), C.GREY)
    lines.append("  " + "  ".join(C.paint(_pad(h, widths[i]), C.BOLD) for i, h in enumerate(headers)))
    lines.append(sep)
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            text = _pad(cell, widths[i], aligns[i])
            color = colors[i] if colors and i < len(colors) else None
            cells.append(C.paint(text, color) if color else text)
        lines.append("  " + "  ".join(cells))
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------- 格式化


def _money(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "-"
    for unit, div in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if abs(x) >= div:
            return f"{x / div:.2f}{unit}"
    return f"{x:.0f}"


def _f(v: Any, digits: int = 2, suffix: str = "") -> str:
    try:
        return f"{float(v):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _signed(v: Any, digits: int = 2) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "-"
    return f"{x:+.{digits}f}"


# ---------------------------------------------------------------- 输出


def output_second_board(results: List[Dict[str, Any]], args: argparse.Namespace,
                        used_cfg=None, notes: Optional[List[str]] = None) -> None:
    if args.json or args.csv:
        _dump(results, args)
        return
    if not results:
        print()
        print("  没有找到符合条件的美人肩（二次涨停）形态。")
        print(C.paint(f"  当前形态条件：涨幅 {used_cfg.anchor_min_gain:g}~{used_cfg.anchor_max_gain:g}% "
                      f"→ 横盘 {used_cfg.min_gap}~{used_cfg.max_gap} 天 "
                      f"→ 收盘震幅 ±{used_cfg.close_band*100:g}% "
                      f"→ 日均振幅 ≤{used_cfg.max_avg_amp*100:g}% "
                      f"→ 量能趋势 ≥{used_cfg.min_vol_trend:g}", C.GREY))
        print(C.paint("  想放宽：--max-gap 10 --close-band 0.05 --min-vol-trend 0.8", C.GREY))
        print(C.paint("  想看全部候选（不做结构过滤）：--no-avoid-fail 或 --min-boards-60d 0", C.GREY))
        print()
        return

    headers = ["#", "代码", "名称", "打分", "启爆概率", "锚点类型", "锚点涨幅%", "首板日", "整理",
               "60日涨停", "现价", "锚点价", "距锚点%", "总振幅%", "收盘位置", "量能趋势"]
    aligns = ["right", "left", "left", "right", "right", "left", "right", "left", "right",
              "right", "right", "right", "right", "right", "right", "right"]
    colors: List[Optional[str]] = [C.GREY, None, None, C.YELLOW, C.MAGENTA, C.CYAN, C.RED,
                                   None, None, None, C.BOLD, None, None, None, None, None]
    rows: List[List[str]] = []
    for r in results:
        rows.append([
            str(r.get("rank", "")),
            r["code"],
            (r.get("name") or "")[:8],
            _f(r["score"], 1),
            C.paint(_f(r.get("prob", 0) * 100, 1, "%"), C.MAGENTA),
            C.paint(r.get("锚点类型") or "-", C.CYAN if r.get("锚点类型") == "涨停" else C.GREY),
            _signed(r.get("锚点涨幅%")),
            str(r["首板日"]),
            str(r["整理天数"]),
            str(r["60日涨停次数"]),
            _f(r["现价"]),
            _f(r["首板价"]),
            _signed(r.get("距首板价%")),
            _f((r.get("range_pct") or 0) * 100 if r.get("range_pct") is not None else None),
            _f(r.get("收盘位置"), 2),
            _f(r.get("量能趋势")),
        ])
    print()
    print(render_table(headers, aligns, rows,
                       title=f"美人肩（二次涨停）形态  ·  命中 {len(results)} 只", colors=colors))
    _print_config(args)

    if notes:
        print(C.paint("  ⚠ 原始条件命中不足，已自动放宽：" + " → ".join(notes), C.YELLOW))
        if used_cfg is not None:
            band = f"±{used_cfg.close_band:.0%}" if used_cfg.close_band else "不限"
            print(C.paint(f"    实际生效: 市场={getattr(args, 'market', 'sz')}  收盘震幅={band}  "
                          f"整理{used_cfg.min_gap}~{used_cfg.max_gap}天  "
                          f"60日涨停≥{used_cfg.min_boards_60d}  "
                          f"量能趋势≥{used_cfg.min_vol_trend}", C.YELLOW))

    print(C.paint("  「启爆概率」= 该打分档位在 2025-2026 年样本中「次日二次涨停」的历史频率（基准 4.8%）。", C.GREY))
    print(C.paint("  规律：60日涨停次数越多 / 整理越短(3~6天) / 量能撑住不缩量 / 股价偏低 / 趋势越强 → 越容易二次涨停。", C.GREY))
    print(C.paint("  反直觉结论：整理期「越窄越横」反而不利（±4% 命中 7.2%，不限 8.5%）——太安静说明没人关注。", C.YELLOW))
    print()
    print(C.paint("  ── 实盘执行要点（研究结论，务必看）─────────────────────────────", C.BOLD))
    print(C.paint("  ① 不要在决策日收盘无脑买入：次日平均低开 0.4%，隔夜持有期望为负。", C.YELLOW))
    print(C.paint("  ② 正确做法：次日开盘观察。低开属常态，开盘/盘中买入，T+1「开盘→收盘」历史期望 +0.3%~+0.8%。", C.GREEN))
    print(C.paint("  ③ 一旦早盘封板成功：3 日内最高价中位数 +15.8%，86% 的案例 3 日内最高涨幅 >10%，不要急于平掉。", C.GREEN))
    print(C.paint("  ④ 失败信号：跌破首板价 5%~8%（研究显示失败组 65% 会触及 -5%），果断止损。", C.RED))
    print(C.paint("  ⑤ 这是「提高命中率」的工具，不是稳赚策略。单票仓位要控制。", C.YELLOW))
    print()


def output_steady_trend(results: List[Dict[str, Any]], args: argparse.Namespace) -> None:
    if args.json or args.csv:
        _dump(results, args)
        return
    if not results:
        print()
        print(C.paint("  今天没有符合条件「趋势票（涨不停不涨停）」。", C.YELLOW))
        print(C.paint("  这类票本来就稀少（历史约每日 0.5~4 只），空结果是正常现象。", C.GREY))
        print(C.paint("  想放宽可以调 --max-distance（默认 1.9；2.5≈每日13只，3.2≈每日47只）。", C.GREY))
        print()
        return

    headers = ["#", "代码", "名称", "相似度", "距离", "摸15%", "现价", "累计涨幅%", "上涨占比%",
               "最大回撤%", "日std", "红柱占比%", "涨跌量比", "距60日高%", "60日涨幅%"]
    aligns = ["right", "left", "left", "right", "right", "right", "right", "right", "right",
              "right", "right", "right", "right", "right", "right"]
    colors: List[Optional[str]] = [C.GREY, None, None, C.MAGENTA, C.YELLOW, C.MAGENTA,
                                   C.BOLD, C.RED, None, None, None, None, None, None, None]
    rows: List[List[str]] = []
    for r in results:
        rows.append([
            str(r.get("rank", "")),
            r["code"],
            (r.get("name") or "")[:8],
            C.paint(_f(r.get("相似度"), 1, "%"), C.MAGENTA),
            _f(r.get("distance"), 2),
            C.paint(_f(r.get("摸15概率", 0) * 100, 1, "%"), C.MAGENTA),
            _f(r["现价"]),
            _signed(r.get("累计涨幅%")),
            _f(r.get("上涨占比%"), 0),
            _f(r.get("最大回撤%")),
            _f(r.get("日涨幅std")),
            _f(r.get("红柱占比%"), 0),
            _f(r.get("涨跌量比")),
            _signed(r.get("距60日高%")),
            _signed(r.get("60日涨幅%"), 0),
        ])
    print()
    print(render_table(headers, aligns, rows,
                       title=f"趋势票（涨不停不涨停）· 命中 {len(results)} 只", colors=colors))
    _print_config(args)
    b = TREND_BASELINE
    print(C.paint("  判定方式：与用户提供的两个样本（002990 盛视科技 / 002655 共达电声）做 14 维相似度匹配，", C.GREY))
    print(C.paint(f"  距离越小越像。基准（全部趋势样本）摸+15% 概率 {b['hit15']:.1%}、20日 {b['ret20']:+.2%}、回撤 {b['min20']:+.2%}。", C.GREY))
    print()
    print(C.paint("  ── 实盘执行要点（研究结论，务必看）─────────────────────────────", C.BOLD))
    print(C.paint("  ① 这是「高赔率」形态：摸到 +15% 的概率约 42%（基准 29%），但上涨率反而略低（~48% vs 53%）。", C.YELLOW))
    print(C.paint("  ② 预期 20 日最大回撤 -10% 左右，比普通趋势股更深，仓位要控制。", C.RED))
    print(C.paint("  ③ 越像模板越强（距离<1.5 的样本 20 日 +3.7%），但样本少、要耐心等。", C.GREEN))
    print(C.paint("  ④ 破位信号：跌破 MA20 或 MACD 柱转绿，说明趋势结束。", C.RED))
    print(C.paint("  ⑤ 历史上大部分日子没有这种票 —— 空结果不是 bug。", C.YELLOW))
    print()


def output_lowpos(results: List[Dict[str, Any]], args: argparse.Namespace) -> None:
    if args.json or args.csv:
        _dump(results, args)
        return
    if not results:
        print()
        print(C.paint("  今天没有符合条件「低位均线粘合（低位票）」。", C.YELLOW))
        print(C.paint("  这种形态本来就要求「低位 + 粘合 + 刚拐头」，空结果是正常现象。", C.GREY))
        print(C.paint("  想放宽：--max-position 0.55 --min-drawdown 0.25 --cross-days 10", C.GREY))
        print()
        return

    headers = ["#", "代码", "名称", "模式", "得分", "现价", "今日%", "3日%", "250位置%", "距高%",
               "250回撤%", "离散度%", "当前离散%", "金叉后", "BIAS20%", "MACD柱", "红柱放大", "量比",
               "市值亿", "成交额亿"]
    aligns = ["right", "left", "left", "left", "right", "right", "right", "right", "right", "right",
              "right", "right", "right", "right", "right", "right", "right", "right", "right", "right"]
    colors: List[Optional[str]] = [C.GREY, None, None, C.CYAN, C.YELLOW, C.BOLD, None, None, C.GREY,
                                   C.GREY, None, C.MAGENTA, C.MAGENTA, C.CYAN, None, None, None, None,
                                   None, None]
    rows: List[List[str]] = []
    for r in results:
        mode = r.get("模式") or "拐头"
        rows.append([
            str(r.get("rank", "")),
            r["code"],
            (r.get("name") or "")[:8],
            C.paint(mode, C.CYAN if mode == "突破" else C.GREEN),
            _f(r.get("score"), 1),
            _f(r.get("现价")),
            _signed(r.get("今日涨跌%") if r.get("今日涨跌%") is not None else r.get("今日涨幅%")),
            _signed(r.get("3日涨幅%")),
            _f(r.get("250日位置%"), 0),
            _signed(r.get("距250日高%"), 1),
            _f(r.get("250日回撤%"), 0),
            _f(r.get("均线离散度%"), 2),
            _f(r.get("当前离散度%"), 2),
            C.paint(str(r.get("金叉后天数", "-")), C.CYAN),
            _signed(r.get("BIAS20%")),
            _f(r.get("MACD柱"), 3),
            str(r.get("红柱放大天数", "-")),
            _f(r.get("量比(5/20)")),
            _f((r.get("总市值") or 0) / 1e8, 0) if r.get("总市值") else "-",
            _f((r.get("成交额") or 0) / 1e8, 2) if r.get("成交额") else "-",
        ])
    print()
    print(render_table(headers, aligns, rows,
                       title=f"低位票（拐头 / 突破）· 命中 {len(results)} 只", colors=colors))
    _print_config(args)
    print(C.paint("  拐头 = 低位粘合 + MA5 刚上穿 MA10 + BIAS20≤8%（买在启动前，历史有正超额）。", C.GREEN))
    print(C.paint("  突破 = 低位粘合 + 放量大阳/涨停 + BIAS20≤20%（用户 2026-10 新样本；", C.CYAN))
    print(C.paint("         适合看图确认，机械追突破在 2021-2026 没有超额，不建议无脑打板）。", C.YELLOW))
    print(C.paint("  实盘排序：同样符合形态时，优先「位置低 + 近20日弱 + 价格低 + 市值小 + 成交额低」的，", C.GREEN))
    print(C.paint("  但 20 日均成交额不要低于 0.2 亿；持仓 4 只，+25% 止盈 / -15% 止损 / 20 个交易日。", C.GREEN))
    print()
    print(C.paint("  ── 历史回测（2021-01 ~ 2026-10，沪深主板，30 万本金）───────────────", C.BOLD))
    print(C.paint("  · 优化版拐头：中证1000>MA20 + 4 只 + 小市值/低成交额排序 + 流动性≥0.2亿/日", C.GREEN))
    print(C.paint("    + +25%止盈 / -15%止损 / 20日：30万 → 145.5万，+384.9%，年化 31.4%，", C.GREEN))
    print(C.paint("    最大回撤 19.2%，221 笔，胜率 55.2%，盈亏比 1.93（训练+99.9% / 验证+141.9%）。", C.GREEN))
    print(C.paint("  · 突破模式：3 个月/6 个月观察可以，但机械次日开盘追突破 = 负期望，", C.YELLOW))
    print(C.paint("    2021-2026 全样本 20 日净收益仅 +0.03%；不要把它当买卖信号。", C.YELLOW))
    print(C.paint("  · 完整逐笔报告：低位票_30万回测_优化版.html（不复权真实价格，含买卖日期/价位/盈亏）。", C.GREY))
    print()
    print(C.paint("  ── 历史统计（2020-2026 沪深主板 3450 只，诚实口径）─────────────", C.BOLD))
    print(C.paint("  · 每日约 20 只进状态（同股 25 日冷却后约 6 只/日新信号）。", C.GREY))
    print(C.paint("  · 全部命中 vs 全市场基准：20 日收益 +3.6% vs +1.9%，涨率 56% vs 52%，", C.GREY))
    print(C.paint("    20 日平均最大回撤 -6.9% vs -8.1%（回撤更浅）—— 相对全市场有正超额。", C.GREEN))
    print(C.paint("  · 摸到 +15% 概率 30.5% vs 27.2%；但「低位基准」是 32.4% —— 这个形态的优势是", C.GREY))
    print(C.paint("    跌得少、期望更高，不是更容易暴涨。不是稳赚策略，仓位要控制。", C.YELLOW))
    print(C.paint("  · 加 --min-vol-trend 1.1：20 日 +4.3%、摸+15% 36.7%（但会漏 002990 那种缩量粘合）。", C.GREEN))
    print(C.paint("  · 分年度 20 日：2020 -5.0% / 2021 +2.8% / 2022 -0.8% / 2023 +1.3% / 2024 +5.8% / 2025 +2.3% / 2026 +2.1%。", C.GREY))
    print()
    print(C.paint("  ── 实盘执行要点 ───────────────────────────────", C.BOLD))
    print(C.paint("  ① 「金叉后」= MA5 上穿 MA10 距今天数（0=当天）。越接近 0~1 越新鲜。", C.GREEN))
    print(C.paint("  ② 买点：次日开盘不再破 MA10，或回踩 MA10/MA20 缩量止跌。", C.GREEN))
    print(C.paint("  ③ 止损：跌破粘合平台下沿 / 跌破 MA20，形态失败。", C.RED))
    print(C.paint("  ④ 止盈：连续大阳后放巨量滞涨，或摸到 +15% 后减半。", C.GREEN))
    print(C.paint("  ⑤ 仓位要控制：该形态平均 20 日最大回撤约 -7%。", C.YELLOW))
    print()


def output_steady(results: List[Dict[str, Any]], args: argparse.Namespace,
                  used_cfg=None, notes: Optional[List[str]] = None) -> None:
    if args.json or args.csv:
        _dump(results, args)
        return
    if not results:
        print("\n  没有找到符合条件的「涨不停不涨停」形态。")
        print(C.paint("  建议：减少 --days（如 5）、放宽 --max-daily-gain，或关闭 --require-ma-bull。\n", C.YELLOW))
        return

    headers = ["#", "代码", "名称", "得分", "现价", "累计涨幅%", "日均%", "单日最大%",
               "单日最大跌%", "阳线%", "量能比", "多头", "换手%", "成交额"]
    aligns = ["right", "left", "left", "right", "right", "right", "right", "right",
              "right", "right", "right", "left", "right", "right"]
    colors: List[Optional[str]] = [C.GREY, None, None, C.YELLOW, C.BOLD, C.RED, None, None,
                                   None, None, None, None, None, None]
    rows: List[List[str]] = []
    for i, r in enumerate(results, 1):
        rows.append([
            str(i),
            r["code"],
            r["name"][:8],
            _f(r["score"], 1),
            _f(r["现价"]),
            _signed(r["累计涨幅%"]),
            _f(r["日均涨幅%"]),
            _f(r["单日最大涨幅%"]),
            _f(r["单日最大跌幅%"]),
            _f(r["阳线占比%"], 0),
            _f(r["量能比"]),
            C.paint("是" if r.get("均线多头") else "否", C.RED if r.get("均线多头") else C.GREY),
            _f(r.get("换手率%")),
            _money(r.get("成交额")),
        ])

    print()
    print(render_table(headers, aligns, rows,
                       title=f"涨不停不涨停  ·  命中 {len(results)} 只", colors=colors))
    _print_config(args)
    if notes:
        print(C.paint("  ⚠ 原始条件无结果，已自动放宽：" + " → ".join(notes), C.YELLOW))
        if used_cfg is not None:
            print(C.paint(f"    实际生效: days={used_cfg.days}  allow_down={used_cfg.allow_down}  "
                          f"max_drawdown={used_cfg.max_drawdown}  "
                          f"require_ma_bull={used_cfg.require_ma_bull}", C.YELLOW))
    print(C.paint("  含义：连续小幅收涨、从不涨停 → 资金持续温和流入，未触发监管与获利盘集中兑现。", C.GREY))
    print(C.paint("  得分越高 = 连涨越久 / 涨幅越均匀 / 均线越多头 / 量能越健康。\n", C.GREY))


def _print_config(args: argparse.Namespace) -> None:
    skip = {"command", "json", "csv", "output", "top", "no_progress", "concurrency",
            "source", "verbose", "refresh", "func", "db", "what", "limit", "help",
            "no_auto_relax", "include_st"}
    keys = {k: v for k, v in vars(args).items() if k not in skip and not callable(v)}
    text = "  ".join(f"{k}={v}" for k, v in keys.items())
    print(C.paint(f"  参数: {text}", C.GREY))


def _dump(results: List[Dict[str, Any]], args: argparse.Namespace) -> None:
    if args.json:
        text = json.dumps(results, ensure_ascii=False, indent=2)
    else:
        cols = sorted({k for r in results for k in r}) if results else []
        buf: List[str] = []
        import io

        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=cols)
        writer.writeheader()
        for r in results:
            writer.writerow(r)
        text = stream.getvalue()
        buf.append(text)

    if args.output:
        with open(args.output, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(text)
        print(f"  已保存到 {args.output}（{len(results)} 条）")
    else:
        print(text)


# ---------------------------------------------------------------- 子命令


async def _prepare_sources(screener: Screener, source: str) -> None:
    """确定数据源顺序：命令行优先，否则用持久化缓存（30 分钟）。"""
    if source:
        screener.svc.force_sources(source.split(","))
        return
    cached = screener.store.get_kv("autoorder:kline:qfq", ttl=1800)
    if cached:
        screener.svc.force_sources(cached)
        return
    order = await screener.svc.auto_order("kline", adjust="qfq", timeout=2.5)
    try:
        screener.store.put_kv("autoorder:kline:qfq", order)
    except Exception:  # noqa: BLE001
        pass


async def run_second_board(args: argparse.Namespace) -> int:
    cfg = SecondBoardConfig(
        min_gap=args.min_gap,
        max_gap=args.max_gap,
        min_boards_60d=args.min_boards_60d,
        min_vol_trend=args.min_vol_trend,
        min_vol_ratio=args.min_vol_ratio,
        max_vol_ratio=args.max_vol_ratio,
        min_price=args.min_price,
        max_price=args.max_price,
        min_avg_amp=args.min_avg_amp / 100.0,
        max_avg_amp=args.max_avg_amp / 100.0,
        min_platform_drift=args.min_platform_drift / 100.0,
        close_band=args.close_band,
        anchor_min_gain=args.anchor_min_gain,
        anchor_max_gain=args.anchor_max_gain,
        max_range_pct=args.max_range,
        min_close_pos=args.min_close_pos,
        min_week_ret_4w=args.min_week_ret,
        require_week_hist_pos=args.require_week_strong,
        min_turnover60=args.min_turnover60,
        min_chip_pct60=args.min_chip_pct,
        min_sec_ret_5d=0.0 if args.require_sector_up else -1e9,
        min_mkt_ret_5d=0.0 if args.require_market_up else -1e9,
        min_mkt_breadth=args.min_breadth,
        avoid_fail=getattr(args, "avoid_fail", False),
        exclude_st=not args.include_st,
    )
    async with Screener(
        concurrency=args.concurrency,
        progress=None if args.no_progress else ConsoleProgress(True),
    ) as screener:
        await _prepare_sources(screener, args.source)
        results, used_cfg, notes = await screener.scan_second_board_ladder(
            cfg, top=args.top, min_score=args.min_score,
            market=args.market, relax=not args.no_auto_relax,
        )
        output_second_board(results, args, used_cfg, notes)
        if args.verbose:
            print(C.paint(f"  数据源: {screener.svc._chain('kline')}", C.GREY))
            print(C.paint(f"  缓存: {screener.store.stats()}", C.GREY))
            print(C.paint(f"  熔断: {screener.svc.breaker_report()}", C.GREY))
    return 0


async def run_steady_trend(args: argparse.Namespace) -> int:
    cfg = SteadyTrendConfig(
        window=args.window,
        max_distance=args.max_distance,
        max_pullback=args.max_pullback,
        exclude_st=not args.include_st,
    )
    async with Screener(
        concurrency=args.concurrency,
        progress=None if args.no_progress else ConsoleProgress(True),
    ) as screener:
        await screener.universe()
        results = await screener.scan_steady_trend(
            cfg, top=args.top, market=args.market,
            exclude_suspended=not args.include_suspended,
        )
        output_steady_trend(results, args)
        if args.verbose:
            print(C.paint(f"  缓存: {screener.store.stats()}", C.GREY))
    return 0


async def run_lowpos(args: argparse.Namespace) -> int:
    cfg = LowPosConfig(
        mode=args.mode,
        min_drawdown=args.min_drawdown,
        max_position=args.max_position,
        max_position_breakout=args.breakout_max_position,
        min_drawdown_120=args.min_drawdown_120,
        max_low_age=args.max_low_age,
        max_spread=args.max_spread,
        max_spread_now=args.max_spread_now,
        min_spread20=args.min_spread20,
        cross_days=args.cross_days,
        min_vol_trend=args.min_vol_trend,
        min_price=args.min_price,
        exclude_st=not args.include_st,
    )
    async with Screener(
        concurrency=args.concurrency,
        progress=None if args.no_progress else ConsoleProgress(True),
    ) as screener:
        await screener.universe()
        results = await screener.scan_low_position(
            cfg, top=args.top, market=args.market,
            exclude_suspended=not args.include_suspended,
        )
        output_lowpos(results, args)
        if args.verbose:
            print(C.paint(f"  缓存: {screener.store.stats()}", C.GREY))
    return 0


async def run_steady(args: argparse.Namespace) -> int:
    cfg = SteadyClimbConfig(
        days=args.days,
        allow_down=args.allow_down,
        max_daily_gain=args.max_daily_gain,
        min_daily_gain=args.min_daily_gain,
        min_total_gain=args.min_total_gain,
        max_total_gain=args.max_total_gain,
        max_drawdown=args.max_drawdown,
        min_bars=args.min_bars,
        exclude_st=not args.include_st,
        min_amount=args.min_amount * 1e4,
        min_price=args.min_price,
        min_market_cap=args.min_market_cap * 1e8,
        min_yang_ratio=args.min_yang_ratio,
        require_ma_bull=args.require_ma_bull,
    )
    async with Screener(
        concurrency=args.concurrency,
        progress=None if args.no_progress else ConsoleProgress(True),
    ) as screener:
        await _prepare_sources(screener, args.source)
        results, used_cfg, notes = await screener.scan_steady_climb_ladder(
            cfg, top=args.top, max_scan=args.max_scan, relax=not args.no_auto_relax
        )
        output_steady(results, args, used_cfg, notes)
        if args.verbose:
            print(C.paint(f"  缓存: {screener.store.stats()}", C.GREY))
            print(C.paint(f"  熔断: {screener.svc.breaker_report()}", C.GREY))
    return 0


async def run_warmup(args: argparse.Namespace) -> int:
    async with Screener(
        concurrency=args.concurrency,
        progress=None if args.no_progress else ConsoleProgress(True),
    ) as screener:
        await _prepare_sources(screener, args.source)
        info = await screener.warmup(limit=args.limit)
        print("\n  预热完成")
        print(f"  请求 {info['requested']} 只 / 成功 {info['success']} 只 / 用时 {info['elapsed']}s")
        print(f"  本地缓存: {info['store']}\n")
    return 0


async def run_refresh(args: argparse.Namespace) -> int:
    async with Screener(
        concurrency=args.concurrency,
        progress=None if args.no_progress else ConsoleProgress(True),
    ) as screener:
        await _prepare_sources(screener, args.source)
        info = await screener.refresh_all(
            adjust=args.adjust, market=args.market, limit=args.limit, force=args.force,
        )
        print("\n  日线更新完成")
        print(f"  上游最新交易日  {info['target_date'] or '--'}")
        print(f"  全市场 {info['universe']} 只 / 落后 {info['lagging']} 只 / "
              f"成功 {info['success']} 只 / 用时 {info['elapsed']}s")
        print(f"  本地缓存: {info['store']}\n")
    return 0


def run_stats(args: argparse.Namespace) -> int:
    store = ScreenerStore(args.db or _default_db())
    info = store.stats()
    print("\n  本地日线缓存")
    for k, v in info.items():
        print(f"    {k:12} {v}")
    print()
    return 0


def run_clear(args: argparse.Namespace) -> int:
    store = ScreenerStore(args.db or _default_db())
    store.clear(args.what)
    print(f"\n  已清空缓存: {args.what}\n")
    return 0


def _default_db() -> str:
    from ..config import get_settings

    return f"{get_settings().data_dir}/screener.db"


# ---------------------------------------------------------------- 参数解析


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--top", type=int, default=30, help="最多输出多少只（默认 30）")
    parser.add_argument("--concurrency", type=int, default=24, help="上游并发数（默认 24）")
    parser.add_argument("--source", default="", help="强制数据源顺序，如 tencent,eastmoney")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--csv", action="store_true", help="输出 CSV")
    parser.add_argument("-o", "--output", default="", help="结果保存到文件（.json / .csv）")
    parser.add_argument("--no-progress", action="store_true", help="不显示进度条")
    parser.add_argument("--no-auto-relax", action="store_true",
                        help="命中为空时不自动放宽条件")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试信息")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-agent-screener",
        description="A 股形态选股器（美人肩 / 涨不停不涨停）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- 美人肩 / 二次涨停 ----
    p = sub.add_parser("secondboard", aliases=["meirenjian", "mrj", "美人肩", "二板"],
                       help="找美人肩（首板→整理→二次涨停）形态")
    p.add_argument("--min-gap", type=int, default=4, help="锚点后至少横盘几天（默认 3）")
    p.add_argument("--max-gap", type=int, default=8,
                   help="锚点后最多横盘几天（默认 15 = 好多天）")
    p.add_argument("--close-band", type=float, default=0.035,
                   help="横盘幅度：整理期每天收盘价必须在锚点价的 ±该比例内（默认 0.07 = ±7%%）")
    p.add_argument("--min-boards-60d", type=int, default=0,
                   help="60 日内最少涨停次数（默认 1；广义口径下只需有过涨停）")
    p.add_argument("--anchor-min-gain", type=float, default=9.4,
                   help="锚点日最小涨幅 %%（默认 9.4 = 涨停/准涨停；回测五年精度峰值）；"
                        "设 4 则回到广义「涨 4 个点以上」）")
    p.add_argument("--anchor-max-gain", type=float, default=10.5,
                   help="锚点日最大涨幅 %%（默认 10.5 = 4%%~10%% 含涨停）")
    p.add_argument("--max-range", type=float, default=0.15,
                   help="横盘期总振幅上限（默认 0.15；设 0 关闭）")
    p.add_argument("--min-close-pos", type=float, default=0.0,
                   help="决策日收盘位置下限（默认 0 = 关闭）")
    p.add_argument("--min-week-ret", type=float, default=0.0,
                   help="近 4 周涨幅下限 %%（默认 0；设 5 可把成功率从 34%% 提到 49%%）")
    p.add_argument("--require-week-strong", action="store_true",
                   help="要求周线 MACD 柱 > 0（周线走强）")
    p.add_argument("--min-turnover60", type=float, default=0.0,
                   help="60 日均换手率下限 %%（默认 0；设 2 过滤死水股）")
    p.add_argument("--min-chip-pct", type=float, default=0.0,
                   help="获利盘分位下限 0~1（默认 0；设 0.5 要求获利盘居多）")
    p.add_argument("--require-sector-up", action="store_true",
                   help="要求所属板块 5 日涨幅 ≥ 0（最强环境因子）")
    p.add_argument("--require-market-up", action="store_true",
                   help="要求大盘 5 日涨幅 ≥ 0")
    p.add_argument("--min-breadth", type=float, default=0.0,
                   help="全市场上涨家数占比下限 0~1（设 0.5 = 一半以上在涨）")
    p.add_argument("--min-vol-trend", type=float, default=0.9,
                   help="量能趋势下限（后2日均量/前2日均量，默认 0.8；不缩量才要）")
    p.add_argument("--min-vol-ratio", type=float, default=0.0,
                   help="整理期均量/首板量 下限（默认 0.50）")
    p.add_argument("--max-vol-ratio", type=float, default=999.0,
                   help="整理期均量/首板量 上限（默认 2.80，放巨量反而差）")
    p.add_argument("--min-price", type=float, default=0.0, help="股价下限（默认不限制）")
    p.add_argument("--max-price", type=float, default=99999.0, help="股价上限（默认不限制）")
    p.add_argument("--min-avg-amp", type=float, default=0.0,
                   help="整理期平均日振幅下限 %%（默认 0=不限制）")
    p.add_argument("--max-avg-amp", type=float, default=4.0,
                   help="整理期平均日振幅上限 %%（默认 6：爬山时「日K只是小波动」）")
    p.add_argument("--min-platform-drift", type=float, default=-1.0,
                   help="平台重心漂移下限 %%（默认 -2：休息平台重心不能向下走）")
    p.add_argument("--min-score", type=float, default=0.0,
                   help="综合打分门槛 0~1（默认 0=全部候选按打分排序）")
    p.add_argument("--market", default="main",
                   choices=["main", "sz", "sh", "all", "cyb", "kcb", "zb", "bj"],
                   help="市场范围（默认 main=沪深主板，已排除创业板/科创板；"
                        "sz=深市全部；sh=沪市全部；all=全市场；zb=深主板；cyb=创业板）")
    p.add_argument("--include-st", action="store_true", help="包含 ST 股")
    p.add_argument("--avoid-fail", action="store_true",
                   help="启用「失败规避」六条规则（默认关闭，保持大道至简；"
                        "开启后命中率更高但更挑剔）")
    p.add_argument("--universe", choices=["all", "limitup"], default="all",
                   help="候选池：all=全市场前复权日线缓存（默认，需先 warmup）")
    _common(p)
    p.set_defaults(func=run_second_board)

    # ---- 趋势票（涨不停不涨停）----
    p = sub.add_parser("trend", aliases=["steady", "ztbt", "涨不停", "趋势票"],
                       help="找趋势票（涨不停不涨停，仿照用户提供的两个样本；默认只扫深交所）")
    p.add_argument("--max-distance", type=float, default=1.90,
                   help="与模板的最大相似距离（默认 1.90≈每日2~3只、摸+15%%约43%%；"
                        "1.50≈每日0.5只、2.50≈每日13只、3.20≈每日47只）")
    p.add_argument("--window", type=int, default=20, help="观察窗口（交易日，默认 20）")
    p.add_argument("--max-pullback", type=float, default=0.12,
                   help="窗口内最大回撤上限（默认 0.12）")
    p.add_argument("--market", default="main",
                   choices=["main", "sz", "sh", "all", "cyb", "kcb", "zb", "bj"],
                   help="市场范围（默认 main=沪深主板，已排除创业板/科创板；"
                        "sz=深市全部；sh=沪市全部；all=全市场）")
    p.add_argument("--include-st", action="store_true", help="包含 ST 股")
    p.add_argument("--include-suspended", action="store_true",
                   help="包含停牌股（默认排除，因为停牌股不可交易）")
    _common(p)
    p.set_defaults(func=run_steady_trend)

    # ---- 低位均线粘合（低位票）----
    p = sub.add_parser("lowpos", aliases=["低位", "低位票", "均线粘合", "dip"],
                       help="找低位均线粘合票（前期暴跌后低位横盘、多根均线粘合、拐头向上）")
    p.add_argument("--mode", default="both", choices=["both", "early", "breakout"],
                   help="both=拐头+突破（默认）；early=只找刚金叉；breakout=只找放量突破")
    p.add_argument("--min-drawdown", type=float, default=0.30,
                   help="250 日区间回撤下限（默认 0.30 = 从 250 日高点到低点至少跌 30%%）")
    p.add_argument("--max-position", type=float, default=0.45,
                   help="拐头模式：250 日区间位置上限（默认 0.45）")
    p.add_argument("--breakout-max-position", type=float, default=0.65,
                   help="突破模式：250 日区间位置上限（默认 0.65）")
    p.add_argument("--min-spread20", type=float, default=0.030,
                   help="突破模式：近 20 日最小离散度上限（默认 0.03）")
    p.add_argument("--min-drawdown-120", type=float, default=0.35,
                   help="近 120 日回撤下限（默认 0.35 = 最近一段暴跌至少 35%%）")
    p.add_argument("--max-low-age", type=int, default=45,
                   help="60 日最低点距今天数上限（默认 45，低点要新鲜）")
    p.add_argument("--max-spread", type=float, default=0.025,
                   help="近 10 日 MA5/10/15/20 最小离散度上限（默认 0.025 = 2.5%%）")
    p.add_argument("--max-spread-now", type=float, default=0.040,
                   help="当前离散度上限（默认 0.04，已拉开的不算）")
    p.add_argument("--cross-days", type=int, default=5,
                   help="MA5 上穿 MA10 的最近天数（默认 5；0 = 只要求 MA5>MA10）")
    p.add_argument("--min-vol-trend", type=float, default=0.0,
                   help="量比(5日/20日) 下限（默认 0；推荐 1.1，收紧后 20 日收益 +4.3%%）")
    p.add_argument("--min-price", type=float, default=2.0, help="股价下限（默认 2 元）")
    p.add_argument("--market", default="main",
                   choices=["main", "sz", "sh", "all", "cyb", "kcb", "zb", "bj"],
                   help="市场范围（默认 main=沪深主板，已排除创业板/科创板）")
    p.add_argument("--include-st", action="store_true", help="包含 ST 股")
    p.add_argument("--include-suspended", action="store_true",
                   help="包含停牌股（默认排除，因为停牌股不可交易）")
    _common(p)
    p.set_defaults(func=run_lowpos)

    # ---- 缓存维护 ----
    p = sub.add_parser("warmup", help="预热全市场日线到本地缓存（扫描提速）")
    p.add_argument("--limit", type=int, default=0, help="只预热前 N 只（0 = 全部）")
    p.add_argument("--concurrency", type=int, default=32)
    p.add_argument("--source", default="")
    p.add_argument("--no-progress", action="store_true")
    p.set_defaults(func=run_warmup, top=0, json=False, csv=False, output="", verbose=False)

    p = sub.add_parser("refresh", aliases=["update", "更新", "增量"],
                       help="把全市场日线增量更新到最新交易日（只补落后的）")
    p.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", "none"])
    p.add_argument("--market", default="main", help="all / main / sh / sz …")
    p.add_argument("--force", action="store_true", help="忽略落后判断，全部重拉")
    p.add_argument("--limit", type=int, default=0, help="只更新前 N 只（0 = 全部）")
    p.add_argument("--concurrency", type=int, default=6,
                   help="并发数（默认 6；调大会很容易被数据源限流）")
    p.add_argument("--source", default="")
    p.add_argument("--no-progress", action="store_true")
    p.set_defaults(func=run_refresh, top=0, json=False, csv=False, output="", verbose=False)

    p = sub.add_parser("stats", help="查看本地缓存状态")
    p.add_argument("--db", default="")
    p.set_defaults(func=run_stats)

    p = sub.add_parser("clear", help="清空本地缓存")
    p.add_argument("--db", default="")
    p.add_argument("--what", default="all", choices=["all", "daily", "kv"])
    p.set_defaults(func=run_clear)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    C.enabled = enable_ansi() and sys.stdout.isatty()
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    args = build_parser().parse_args(argv)
    try:
        if asyncio.iscoroutinefunction(args.func):
            return asyncio.run(args.func(args))
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\n  错误: {exc}\n", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
