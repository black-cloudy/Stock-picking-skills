"""研究步骤 31：把组合回测结果渲染成 HTML 交易报告。

用法::

    python research/03_backtest/31_report.py                       # 用最优变体
    python research/03_backtest/31_report.py --variant "H1 ..."    # 指定变体
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from typing import Any, Dict, List

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "stock_agent")) and os.path.dirname(_p) != _p:
    _p = os.path.dirname(_p)
ROOT = _p
SRC = os.path.join(ROOT, ".data", "sim_raw_classic.json")
OUT = os.path.join(ROOT, ".data", "美人肩_30万回测报告.html")
TITLES = {"classic": "经典美人肩（涨停 → 横盘 → 二次涨停）",
          "broad": "广义美人肩（大涨 → 横盘 → 涨停）"}

HEAD = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>美人肩 · 30万实盘模拟回测报告</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e15;color:#e7ecf5;font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;padding:26px 30px 60px}
h1{font-size:22px;margin-bottom:4px}
.sub{color:#7d8799;font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:22px}
.card{background:#141924;border:1px solid #232b3a;border-radius:12px;padding:14px 16px}
.card span{display:block;font-size:11.5px;color:#7d8799;margin-bottom:6px}
.card i{font-style:normal;font-size:22px;font-weight:800}
.up{color:#ff4d3d}.dn{color:#00c076}.acc{color:#ffb347}.blue{color:#4a9eff}
#chart{height:380px;background:#141924;border:1px solid #232b3a;border-radius:12px;margin-bottom:22px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{position:sticky;top:0;background:#1a202c;color:#9aa5b8;font-weight:600;text-align:left;padding:9px 10px;border-bottom:1px solid #232b3a;cursor:pointer;white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid #1a202c;white-space:nowrap}
tbody tr:hover{background:#171d29}
.wrap{max-height:640px;overflow:auto;border:1px solid #232b3a;border-radius:12px}
.note{background:linear-gradient(90deg,rgba(255,138,61,.1),transparent);border:1px solid rgba(255,138,61,.28);border-radius:10px;padding:12px 16px;font-size:12.5px;color:#9aa5b8;margin-bottom:20px}
.note b{color:#ffb347}
.tag{padding:1px 7px;border-radius:5px;font-size:11px}
.t-win{background:rgba(255,77,61,.15);color:#ff4d3d}
.t-loss{background:rgba(0,192,118,.15);color:#00c076}
h2{font-size:15px;margin:22px 0 10px;display:flex;align-items:center;gap:8px}
h2::before{content:"";width:3px;height:14px;background:#ff8a3d;border-radius:2px}
</style></head><body>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="")
    ap.add_argument("--tag", default="classic", choices=["classic", "broad"])
    args = ap.parse_args()

    global SRC, OUT
    SRC = os.path.join(ROOT, ".data", f"sim_raw_{args.tag}.json")
    OUT = os.path.join(ROOT, ".data", f"美人肩_{'经典' if args.tag=='classic' else '广义'}_30万回测.html")

    data = json.load(open(SRC, encoding="utf-8"))
    key = args.variant or max(data, key=lambda k: data[k]["return_pct"])
    r = data[key]
    trades: List[Dict[str, Any]] = r["trades"]
    curve = r["curve"]

    # 年度盈亏
    year_pnl: Dict[str, float] = defaultdict(float)
    year_n: Dict[str, int] = defaultdict(int)
    for t in trades:
        y = t["sell_date"][:4]
        year_pnl[y] += t["pnl"]
        year_n[y] += 1

    rows = []
    for i, t in enumerate(trades, 1):
        cl = "up" if t["pnl"] > 0 else "dn"
        tag = "t-win" if t["pnl"] > 0 else "t-loss"
        rows.append(
            f"<tr><td>{i}</td><td>{t['code']}</td><td>{t['name']}</td>"
            f"<td>{t['buy_date']}</td><td>{t['buy']:.2f}</td><td>{t['shares']:,}</td>"
            f"<td>{t['amount_in']:,.0f}</td><td>{t['sell_date']}</td><td>{t['sell']:.2f}</td>"
            f"<td>{t['amount_out']:,.0f}</td>"
            f"<td class='{cl}'>{t['pnl']:+,.0f}</td>"
            f"<td><span class='tag {tag}'>{t['ret']:+.2f}%</span></td>"
            f"<td>{t['days']}</td><td>{t['reason']}</td></tr>")

    yrows = "".join(
        f"<tr><td>{y}</td><td>{year_n[y]}</td>"
        f"<td class='{'up' if year_pnl[y]>0 else 'dn'}'>{year_pnl[y]:+,.0f}</td></tr>"
        for y in sorted(year_pnl))

    ret_cls = "up" if r["return_pct"] > 0 else "dn"
    html = HEAD + f"""
<h1>美人肩 · 30 万实盘模拟回测报告</h1>
<div class="sub" style="color:#ffb347;font-weight:700">{TITLES[args.tag]}</div>
<div class="sub">变体：<b>{key}</b> ｜ 区间 2021-01-04 ~ {curve[-1]['date']} ｜ 最多同时持有 5 只 ｜ 次日开盘成交 ｜ 佣金+印花税已计</div>

<div class="note">
<b>说明：</b>这是机械化执行的回测结果。<b>第 1 天收盘选股，第 2 天开盘买入</b>；卖出为次日开盘执行。
本报告不修饰亏损，用于评估该形态在真实成本下的表现。
</div>

<div class="grid">
  <div class="card"><span>初始本金</span><i>300,000</i></div>
  <div class="card"><span>期末权益</span><i class="acc">{r['final']:,.0f}</i></div>
  <div class="card"><span>总收益</span><i class="{ret_cls}">{r['return_pct']:+.2f}%</i></div>
  <div class="card"><span>最大回撤</span><i class="dn">{r['mdd']:.2f}%</i></div>
  <div class="card"><span>交易笔数</span><i>{r['n_trades']}</i></div>
  <div class="card"><span>胜率</span><i class="blue">{r['win_rate']:.1f}%</i></div>
  <div class="card"><span>平均盈利</span><i class="up">{r['avg_win']:+.2f}%</i></div>
  <div class="card"><span>平均亏损</span><i class="dn">{r['avg_loss']:+.2f}%</i></div>
  <div class="card"><span>盈亏比(PF)</span><i>{r['profit_factor']:.2f}</i></div>
  <div class="card"><span>平均持有</span><i>{r['avg_days']:.1f} 天</i></div>
</div>

<h2>净值曲线</h2>
<div id="chart"></div>

<h2>年度盈亏</h2>
<div class="wrap" style="max-height:260px"><table>
<thead><tr><th>年份</th><th>交易笔数</th><th>盈亏(元)</th></tr></thead>
<tbody>{yrows}</tbody></table></div>

<h2>全部交易明细（{len(trades)} 笔）</h2>
<div class="wrap"><table>
<thead><tr><th>#</th><th>代码</th><th>名称</th><th>买入日期</th><th>买入价</th><th>股数</th>
<th>买入金额</th><th>卖出日期</th><th>卖出价</th><th>卖出金额</th><th>盈亏(元)</th><th>收益率</th><th>持有天数</th><th>退出原因</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>

<script>
const d = {json.dumps([c['date'] for c in curve])};
const e = {json.dumps([c['equity'] for c in curve])};
const ch = echarts.init(document.getElementById('chart'));
ch.setOption({{
  animation:false, backgroundColor:'transparent',
  grid:{{left:70,right:30,top:24,bottom:40}},
  tooltip:{{trigger:'axis',backgroundColor:'rgba(20,25,36,.96)',borderColor:'#232b3a',textStyle:{{color:'#e7ecf5'}}}},
  xAxis:{{type:'category',data:d,axisLine:{{lineStyle:{{color:'#232b3a'}}}},axisLabel:{{color:'#66718a'}}}},
  yAxis:{{type:'value',scale:true,splitLine:{{lineStyle:{{color:'rgba(35,43,58,.6)'}}}},axisLabel:{{color:'#66718a'}}}},
  series:[{{name:'权益',type:'line',data:e,showSymbol:false,smooth:true,lineStyle:{{width:2,color:'#ffb347'}},
    areaStyle:{{color:new echarts.graphic.LinearGradient(0,0,0,1,[{{offset:0,color:'rgba(255,179,71,.35)'}},{{offset:1,color:'rgba(255,179,71,0)'}}])}}}}]
}});
window.addEventListener('resize',()=>ch.resize());
</script>
</body></html>"""

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"已生成报告: {OUT}")
    print(f"变体 {key}: 期末 {r['final']:,.0f}  收益 {r['return_pct']:+.2f}%  交易 {r['n_trades']} 笔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
