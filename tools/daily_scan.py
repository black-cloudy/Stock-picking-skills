"""每日美人肩巡检：输出「严格口径」和「观察池」两栏。

* **严格口径** —— 与回测验证过的参数完全一致（经典 + 广义两个子口径），可直接执行
  经典 2021-2026 实测 +63.9% / 回撤 14.4%；广义 +122.0% / 回撤 28.0%
* **观察池** —— 参数主动放宽（横盘 3~10 天、震幅 ±5%、日振幅 ≤7%、量能 ≥0.8），
  只为「有票可看」，**每条都标注偏离了哪一项**，不可直接照做

用法::

    python tools/daily_scan.py                  # 用现有缓存扫
    python tools/daily_scan.py --refresh        # 先刷新日线（约 20~25 分钟）
    python tools/daily_scan.py --popup          # 有票时弹窗

建议用 Windows 计划任务每天 16:10 自动跑（收盘后）::

    tools\\register_task.bat
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, List

# ---------------------------------------------------------------- 路径引导
_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
sys.path.insert(0, _p)
ROOT = _p

from stock_agent.screener.patterns import SecondBoardConfig  # noqa: E402
from stock_agent.screener.scanner import Screener  # noqa: E402

ALERT_DIR = os.path.join(ROOT, ".data", "alerts")
STOP_BAND = 0.07


# ---------------------------------------------------------------- 行数据
def plan_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """补上作战参数：止损 / 目标 / 盈亏比。"""
    price = float(r.get("现价") or 0)
    anchor = float(r.get("首板价") or 0)
    stop = round(anchor * (1 - STOP_BAND), 2)
    target = round(price * 1.10, 2)
    rr = round((target - price) / max(price - stop, 0.01), 2)
    gain = float(r.get("锚点涨幅%") or 0)
    return {
        "code": r.get("code"), "name": r.get("name"),
        "price": price, "anchor": anchor, "anchor_gain": round(gain, 2),
        "anchor_type": "涨停" if gain >= 9.4 else "大涨",
        "gap": r.get("整理天数"),
        "band": round(max(abs(float(r.get("收盘下偏%") or 0)),
                          abs(float(r.get("收盘上偏%") or 0))), 1),
        "amp": round(float(r.get("平均振幅%") or 0), 1),
        "vol": round(float(r.get("量能趋势") or 0), 2),
        "score": r.get("score"), "prob": round(float(r.get("prob") or 0) * 100, 1),
        "boards60": r.get("60日涨停次数"),
        "stop": stop, "target": target, "rr": rr,
        "board_date": r.get("首板日"),
        "dev": [],
    }


def deviations_of(r: Dict[str, Any], st: SecondBoardConfig) -> List[str]:
    """相对「严格口径」偏离了哪些条件。"""
    out: List[str] = []
    gap = int(r.get("gap") or 0)
    if not (st.min_gap <= gap <= st.max_gap):
        out.append(f"横盘{gap}天")
    if r.get("band", 0) > st.close_band * 100 + 1e-9:
        out.append(f"震幅{r['band']:.1f}%")
    if r.get("amp", 0) > st.max_avg_amp * 100 + 1e-9:
        out.append(f"日振幅{r['amp']:.1f}%")
    if r.get("vol", 0) < st.min_vol_trend - 1e-9:
        out.append(f"量能{r['vol']:.2f}")
    return out


def md_table(rows: List[Dict[str, Any]], with_dev: bool = False) -> List[str]:
    if not rows:
        return ["**命中：0 只** —— 今天没有符合条件的票，空仓。", ""]
    head = ("| 代码 | 名称 | 类型 | 现价 | 锚点 | 锚点涨% | 横盘 | 震幅 | 日振幅 | 量能 | "
            "打分 | 概率 | 止损 | 目标 | 盈亏比 |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    if with_dev:
        head += " 偏离项 |"
        sep += "---|"
    out = [f"**命中：{len(rows)} 只**", "", head, sep]
    for r in rows:
        line = (f"| {r['code']} | {r['name']} | {r['anchor_type']} | {r['price']:.2f} | "
                f"{r['anchor']:.2f} | {r['anchor_gain']:.2f} | {r['gap']}天 | {r['band']:.1f}% | "
                f"{r['amp']:.1f}% | {r['vol']:.2f} | {r['score']} | {r['prob']}% | "
                f"{r['stop']:.2f} | {r['target']:.2f} | {r['rr']:.2f} |")
        if with_dev:
            line += f" {'、'.join(r['dev']) or '—'} |"
        out.append(line)
    out.append("")
    return out


def render_md(date: str, classic: List[Dict], broad: List[Dict],
              watch: List[Dict], data_date: str, elapsed: float) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L = [f"# 美人肩巡检 · {date}", "",
         f"- 运行时间：{now}",
         f"- 数据日期：**{data_date}**（收盘）",
         f"- 耗时：{elapsed:.0f} 秒", ""]

    L += ["---", "", "# ① 严格口径（与回测一致，可直接执行）", "",
          "## 经典美人肩（涨停 → 横盘 → 二次涨停）",
          "锚点 9.4~10.5% ｜ 横盘 4~8 天 ｜ 震幅 ±3.5% ｜ 日振幅 ≤4% ｜ 量能 ≥0.9",
          "> 2021-2026 实测：+63.9% / 最大回撤 14.4% / 胜率 48.8% / PF 1.29", ""]
    L += md_table(classic)

    L += ["## 广义美人肩（大涨 → 横盘 → 涨停）",
          "锚点 4~9.4% ｜ 横盘 3~8 天 ｜ 震幅 ±4% ｜ 日振幅 ≤4% ｜ 量能 ≥0.9",
          "> 2021-2026 实测：+122.0% / 最大回撤 28.0% / 胜率 48.3% / PF 1.16（波动较大）", ""]
    L += md_table(broad)

    L += ["---", "", "# ② 观察池（参数已放宽，**仅供观察**）", "",
          "> ⚠️ 以下标的**偏离了回测口径**：横盘 3~10 天、震幅 ±5%、日振幅 ≤7%、量能 ≥0.8。",
          "> 放宽后回测收益会显著恶化（横盘放到 3~15 天时为 **-68.6%**），**不可直接照做**。",
          "> 「偏离项」列标注了它具体超出了哪一条严格条件。", ""]
    L += md_table(watch, with_dev=True)

    L += ["---", "",
          "> **执行提醒**：收盘选股 → 次日开盘观察再决定买入；止损 = 锚点价 × 0.93；单票 ≤20% 仓位。",
          "> 严格口径年均只有约 50 次机会，**没有票就是没有票**，观察池不能替代。"]
    return "\n".join(L)


# ---------------------------------------------------------------- 辅助
def popup(title: str, text: str, seconds: int = 30) -> None:
    """Windows 弹窗提醒（自动关闭）。"""
    try:
        ps = ("$w = New-Object -ComObject WScript.Shell; "
              f"$w.Popup('{text}', {seconds}, '{title}', 64) | Out-Null")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       timeout=seconds + 5, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        print(f"  （弹窗失败：{exc}）")


def refresh_data() -> bool:
    script = os.path.join(ROOT, "research", "02_data", "28_refresh_qfq.py")
    print("① 刷新日线数据（约 20~25 分钟）…")
    try:
        return subprocess.run([sys.executable, script, "--concurrency", "40"],
                              cwd=ROOT, timeout=3600).returncode == 0
    except Exception as exc:  # noqa: BLE001
        print(f"  刷新失败：{exc}")
        return False


async def run_scans(refresh: bool) -> Dict[str, Any]:
    if refresh:
        refresh_data()

    classic_cfg = SecondBoardConfig()                     # 9.4~10.5 严格
    broad_cfg = replace(classic_cfg, anchor_min_gain=4.0, anchor_max_gain=9.4,
                        min_gap=3, close_band=0.04)       # 4~9.4 严格
    watch_cfg = replace(classic_cfg, anchor_min_gain=4.0, anchor_max_gain=10.5,
                        min_gap=3, max_gap=10, close_band=0.05,
                        max_avg_amp=0.07, min_vol_trend=0.8)   # 观察池

    async with Screener(concurrency=16) as sc:
        data_date = (sc.store.recent_dates("qfq", 1) or [""])[0]
        classic = await sc.scan_second_board(classic_cfg, top=30, market="main", adjust="qfq")
        broad = await sc.scan_second_board(broad_cfg, top=30, market="main", adjust="qfq")
        watch = await sc.scan_second_board(watch_cfg, top=40, market="main", adjust="qfq")
        return {"data_date": data_date, "classic": classic, "broad": broad, "watch": watch}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="先刷新日线（约 20~25 分钟）")
    ap.add_argument("--popup", action="store_true", help="严格口径有票时弹窗")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--pool", choices=["all", "strict", "watch"], default="all",
                    help="只看某一栏：all=全部（默认） strict=只看严格口径 watch=只看观察池")
    args = ap.parse_args()

    t0 = datetime.now()
    info = asyncio.run(run_scans(args.refresh))
    elapsed = (datetime.now() - t0).total_seconds()

    strict_cfg = SecondBoardConfig()
    classic = [plan_row(r) for r in info["classic"][: args.top]]
    broad = [plan_row(r) for r in info["broad"][: args.top]]
    watch = [plan_row(r) for r in info["watch"][: args.top]]
    for r in watch:
        r["dev"] = deviations_of(r, strict_cfg)
    # 观察池：偏得最少的排前面
    watch.sort(key=lambda r: (len(r["dev"]), -float(r["score"] or 0)))

    date = datetime.now().strftime("%Y-%m-%d")
    md = render_md(date, classic, broad, watch, info["data_date"], elapsed)
    os.makedirs(ALERT_DIR, exist_ok=True)
    for name in (f"{date}.md", "latest.md"):
        with open(os.path.join(ALERT_DIR, name), "w", encoding="utf-8") as fh:
            fh.write(md)
    with open(os.path.join(ALERT_DIR, "latest.json"), "w", encoding="utf-8") as fh:
        json.dump({"date": date, "data_date": info["data_date"],
                   "classic": classic, "broad": broad, "watch": watch},
                  fh, ensure_ascii=False, indent=1)

    # ---------------- 控制台 ----------------
    print()
    print(f"数据日期 {info['data_date']}   耗时 {elapsed:.0f}s")
    print()
    show_strict = args.pool in ("all", "strict")
    show_watch = args.pool in ("all", "watch")

    if show_strict:
        print("═══ ① 严格口径（可直接执行）═══")
        print(f"  经典美人肩：{len(classic)} 只")
        for r in classic:
            print(f"    {r['code']} {r['name']:<8} {r['price']:>8.2f}  横盘{r['gap']}天 "
                  f"震幅{r['band']:.1f}% 量能{r['vol']:.2f}  分{r['score']:>5} 概率{r['prob']:>5}%  "
                  f"止损{r['stop']:.2f} 目标{r['target']:.2f}")
        print(f"  广义美人肩：{len(broad)} 只")
        for r in broad:
            print(f"    {r['code']} {r['name']:<8} {r['price']:>8.2f}  锚点涨{r['anchor_gain']:>5.2f}% "
                  f"横盘{r['gap']}天 量能{r['vol']:.2f}  分{r['score']:>5} 概率{r['prob']:>5}%")
        print()

    if show_watch:
        print("═══ ② 观察池（参数已放宽，仅供参考）═══")
        print(f"  共 {len(watch)} 只（按偏离项数量排序，越靠前越接近严格口径）")
        for r in watch[:15]:
            print(f"    {r['code']} {r['name']:<8} {r['price']:>8.2f} {r['anchor_type']} "
                  f"横盘{r['gap']:>2}天 震幅{r['band']:>4.1f}% 日振幅{r['amp']:>4.1f}% "
                  f"量能{r['vol']:.2f} 分{r['score']:>5}  ← 偏离: {'、'.join(r['dev']) or '无'}")
        print()
    print(f"报告：{os.path.join(ALERT_DIR, f'{date}.md')}")

    if args.popup and (classic or broad):
        names = "、".join(r["name"] for r in (classic + broad)[:4])
        popup("美人肩巡检 · 严格口径有票",
              f"经典 {len(classic)} 只 / 广义 {len(broad)} 只\\n{names}", 30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
