/* ══════════════════════════════════════════════════════════════
   美人肩 · 前端逻辑（ECharts K线 + MACD + 成交量）
   ══════════════════════════════════════════════════════════════ */
'use strict';

const UP = '#ff4d3d', DN = '#00c076', TXT2 = '#9aa5b8', TXT3 = '#66718a',
      LINE = '#232b3a', ACC = '#ff8a3d';

const state = { items: [], filtered: [], current: null, chart: null, loading: false };

const $ = (id) => document.getElementById(id);
const f2 = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '--' : Number(v).toFixed(d);
const cls = (v) => v > 0 ? 'up' : (v < 0 ? 'dn' : 'flat');
const sgn = (v) => (v > 0 ? '+' : '') + f2(v);

function toast(msg, ms = 2200) {
  const t = $('toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, ms);
}

/* ─────────────── 头部统计 ─────────────── */
async function loadStats() {
  try {
    const r = await fetch('/api/mrj/stats').then(r => r.json());
    const b = r.backtest || {};
    $('sbBacktest').innerHTML =
      `五年实测：选中 <b>${(b.selected||0).toLocaleString()}</b> 次 / ` +
      `二板成功 <b>${(b.hits||0).toLocaleString()}</b> 次 / ` +
      `最终兑现率 <b>${b.hit_rate}%</b>`;
    $('sbWindow').innerHTML = `最佳潜伏窗口：首板后第 <b>3 ~ 15</b> 个交易日`;
    $('mcMetrics').innerHTML = `
      <div class="m"><i>${b.hit_rate}%</i><span>最终兑现率</span></div>
      <div class="m"><i>${b.signal_precision}%</i><span>信号精度</span></div>
      <div class="m"><i>${b.recall}%</i><span>召回率</span></div>
      <div class="m"><i>${b.per_day}</i><span>日均候选</span></div>`;
    $('mcText').innerHTML =
      `一只股票涨停后横盘数日、量能不放不缩、股价钉在首板价附近 —— 这是资金洗盘的典型特征。<br>` +
      `本系统扫描该形态，在<b>二次涨停爆发前</b>给出候选与潜伏区间。` +
      `五年（${b.window}）回测：选中标的最终 <b>${b.hit_rate}%</b> 兑现二次涨停、` +
      `<b>${b.break_up}%</b> 冲高突破 +7%、<b>${b.break_down}%</b> 跌破下沿。`;
  } catch (e) { console.warn(e); }
}

/* ─────────────── 扫描 ─────────────── */
function scanParams() {
  const market = document.querySelector('#fMarket button.active')?.dataset.m || 'main';
  return {
    market,
    min_gap: 3,
    max_gap: +$('fGap').value,
    max_amp: +$('fAmp').value,
    min_vol: +$('fVol').value,
    limit: 60,
  };
}

async function runScan(refresh = false, forceBars = false) {
  if (state.loading) return;
  state.loading = true;
  $('cards').innerHTML = `<div class="empty"><span class="spin">◌</span> ` +
    (forceBars ? '正在从上游重新拉取日线…（约 1~2 分钟）' : '正在扫描沪深A股…') + `</div>`;
  $('chipTime').textContent = '扫描中…';
  const p = scanParams();
  const qs = new URLSearchParams({ ...p, refresh: refresh ? 1 : 0, force_bars: forceBars ? 1 : 0 });
  try {
    const r = await fetch('/api/mrj/scan?' + qs).then(r => r.json());
    if (!r.ok) throw new Error(r.detail || '扫描失败');
    state.items = r.items || [];
    $('sbDate').textContent = `数据日期：${r.trade_date || '--'}`;
    $('chipCount').textContent = `候选 ${r.total} 只`;
    $('chipTime').textContent = `${r.cached ? '缓存' : '耗时 ' + r.elapsed + 's'}`;
    applyFilter();
    if (state.filtered.length && !state.current) selectStock(state.filtered[0].code);
  } catch (e) {
    $('cards').innerHTML = `<div class="empty">扫描失败：${e.message}</div>`;
    $('chipTime').textContent = '失败';
  } finally { state.loading = false; }
}

/* ─────────────── 过滤 + 渲染卡片 ─────────────── */
function applyFilter() {
  const kw = ($('fSearch').value || '').trim().toLowerCase();
  state.filtered = state.items.filter(it => {
    if (!kw) return true;
    return (it.code + it.name + (it.industry || '')).toLowerCase().includes(kw);
  });
  $('listHint').textContent = `共 ${state.filtered.length} 只 · 按综合打分排序`;
  renderCards();
}

function renderCards() {
  const box = $('cards');
  if (!state.filtered.length) { box.innerHTML = `<div class="empty">没有符合条件的标的，试试放宽过滤条件</div>`; return; }
  box.innerHTML = state.filtered.map(it => {
    const c = cls(it.change_pct || 0);
    const isLU = it.status === '二板已成';
    const note = isLU
      ? `【二板已成】今日已封涨停，形态兑现`
      : `【临界爆发】横盘第 ${it.gap} 天，量能趋势 ${f2(it.vol_trend)}${it.vol_trend >= 1 ? '（放量）' : ''}`;
    return `
    <div class="card${isLU ? ' lu' : ''}${state.current === it.code ? ' active' : ''}" data-code="${it.code}">
      <div class="c-top">
        <div>
          <div>
            <span class="c-code">${it.code}</span>
            <span class="c-name">${it.name || ''}</span>
            <span class="c-badge${isLU ? '' : ' warn'}">${it.status}</span>
          </div>
          <div class="c-ind">${it.industry || '—'}</div>
        </div>
        <div class="c-price">
          <div class="p ${c}">${f2(it.price)}</div>
          <span class="pc ${c}">${sgn(it.change_pct)}%</span>
        </div>
      </div>
      <div class="c-stats">
        <div class="s"><i>${it.gap}</i><span>蓄势天数</span></div>
        <div class="s"><i>${f2(it.avg_amp)}%</i><span>日均振幅</span></div>
        <div class="s"><i>${f2(it.vol_trend)}</i><span>量能趋势</span></div>
        <div class="s"><i>${f2(it.score, 1)}</i><span>匹配得分</span></div>
      </div>
      <div class="c-note">${note} · 二次涨停概率 <b>${f2(it.prob, 1)}%</b></div>
    </div>`;
  }).join('');
  box.querySelectorAll('.card').forEach(el =>
    el.addEventListener('click', () => selectStock(el.dataset.code)));
}

/* ─────────────── 详情 ─────────────── */
async function selectStock(code) {
  state.current = code;
  renderCards();
  const panel = $('detail');
  panel.innerHTML = `<div class="empty big"><span class="spin">◌</span> 加载 ${code} 详情…</div>`;
  try {
    const r = await fetch(`/api/mrj/detail?code=${code}&close_band=0.07`).then(r => r.json());
    if (!r.ok) throw new Error(r.detail || '加载失败');
    renderDetail(r);
  } catch (e) {
    panel.innerHTML = `<div class="empty big">${e.message}</div>`;
  }
}

function renderDetail(d) {
  const it = state.items.find(x => x.code === d.code) || {};
  const dt = d.detail || {}, plan = d.plan || {};
  const c = cls(dt.change_pct || 0);
  const gap = dt.gap ?? it.gap ?? '--';

  $('detail').innerHTML = `
    <div class="d-head">
      <div class="d-title">
        <span class="code">${d.code}</span>
        <span class="nm">${d.name || it.name || ''}</span>
        <span class="sub">处于首板涨停后第 <b>${gap}</b> 天横盘蓄势</span>
      </div>
      <div class="d-ma">
        <span>MA5 <b>${f2(it.ma5)}</b></span>
        <span>MA10 <b>${f2(it.ma10)}</b></span>
        <span>MA20 <b>${f2(it.ma20)}</b></span>
        <span>匹配得分 <b>${f2(it.score, 1)}</b></span>
        <span>二次涨停概率 <b>${f2(it.prob, 1)}%</b></span>
      </div>
    </div>

    <div class="stat-row">
      <div class="s"><span>日期</span><i>${dt.date || '--'}</i></div>
      <div class="s"><span>收盘</span><i class="${c}">${f2(dt.close)}</i></div>
      <div class="s"><span>涨跌</span><i class="${c}">${sgn(dt.change_pct)}%</i></div>
      <div class="s"><span>日均振幅</span><i>${f2(dt.amplitude)}%</i></div>
      <div class="s"><span>成交量</span><i>${dt.volume ? (dt.volume / 1e4).toFixed(1) + '万' : '--'}</i></div>
      <div class="s"><span>DIF</span><i>${f2(dt.dif, 3)}</i></div>
      <div class="s"><span>DEA</span><i>${f2(dt.dea, 3)}</i></div>
      <div class="s"><span>首板价</span><i>${f2(plan.anchor_price)}</i></div>
    </div>

    <div class="chart" id="chart"></div>

    <div class="d-note">
      <b>形态节点诊断（${dt.date || ''}）：</b>
      首板日 ${plan.anchor_date || '--'} 涨停启动 → 横盘 <b>${gap}</b> 天蓄势，
      量能趋势 <b>${f2(it.vol_trend)}</b>，收盘距首板价 <b>${sgn(it.dev_close_last)}%</b>，
      整理期总振幅 <b>${f2(it.range_pct, 1)}%</b>。
    </div>

    <div class="d-sec">
      <h3>美人肩实战潜伏作战手册</h3>
      <div class="plan-grid">
        <div class="p buy"><span>建仓潜伏区间</span><i>${f2(plan.buy_range?.[0])} ~ ${f2(plan.buy_range?.[1])}</i><em>锚点价附近低吸</em></div>
        <div class="p stop"><span>严格止损线</span><i>${f2(plan.stop)}</i><em>锚点价 × 0.93，破位离场</em></div>
        <div class="p target"><span>二次涨停目标位</span><i>${f2(plan.target)}</i><em>现价 × 1.10</em></div>
        <div class="p rr"><span>预期盈亏比</span><i>1 : ${f2(plan.rr, 1)}</i><em>目标空间 / 止损空间</em></div>
      </div>
    </div>

    <div class="d-sec">
      <h3>量化特征契合度全息体检</h3>
      <div class="checks">
        ${(d.checks || []).map(k => `
          <div class="k ${k.ok ? 'ok' : 'no'}">
            <span class="mk">${k.ok ? '✓' : '✕'}</span>
            <span class="lb">${k.label}</span>
            <span class="dt">${k.detail || ''}</span>
          </div>`).join('')}
      </div>
    </div>`;

  drawChart(d);
}

/* ─────────────── ECharts ─────────────── */
function drawChart(d) {
  const el = $('chart');
  if (!el || !window.echarts) return;
  if (state.chart) { state.chart.dispose(); state.chart = null; }
  const chart = echarts.init(el, null, { renderer: 'canvas' });
  state.chart = chart;

  const K = d.kline || [];
  const dates = K.map(x => String(x.date).slice(5));
  const ohlc = K.map(x => [x.open, x.close, x.low, x.high]);
  const vols = K.map((x, i) => ({
    value: x.volume,
    itemStyle: { color: (x.close >= x.open) ? 'rgba(255,77,61,.55)' : 'rgba(0,192,118,.55)' },
  }));
  const dif = (d.dif || []).map(v => v === null ? '-' : +Number(v).toFixed(3));
  const dea = (d.dea || []).map(v => v === null ? '-' : +Number(v).toFixed(3));
  const hist = (d.hist || []).map(v => ({
    value: v === null ? '-' : +Number(v).toFixed(3),
    itemStyle: { color: (v >= 0 ? 'rgba(255,77,61,.75)' : 'rgba(0,192,118,.75)') },
  }));

  const ai = d.anchor_index ?? 0;
  const cs = d.consolidation || [ai + 1, K.length - 1];

  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    textStyle: { color: TXT2, fontSize: 11 },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross', lineStyle: { color: '#3a465c' } },
      backgroundColor: 'rgba(20,25,36,.96)', borderColor: LINE, textStyle: { color: '#e7ecf5', fontSize: 11.5 },
      formatter: (ps) => {
        const i = ps[0].dataIndex, k = K[i];
        return `<b>${k.date}</b><br/>开 ${f2(k.open)} 收 ${f2(k.close)}<br/>低 ${f2(k.low)} 高 ${f2(k.high)}<br/>` +
               `量 ${(k.volume / 1e4).toFixed(1)}万手`;
      },
    },
    grid: [
      { left: 58, right: 22, top: 26, height: '50%' },
      { left: 58, right: 22, top: '63%', height: '11%' },
      { left: 58, right: 22, top: '79%', height: '15%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, boundaryGap: true, axisLine: { lineStyle: { color: LINE } }, axisLabel: { show: false }, axisTick: { show: false } },
      { type: 'category', data: dates, gridIndex: 1, boundaryGap: true, axisLine: { lineStyle: { color: LINE } }, axisLabel: { show: false }, axisTick: { show: false } },
      { type: 'category', data: dates, gridIndex: 2, boundaryGap: true, axisLine: { lineStyle: { color: LINE } }, axisLabel: { color: TXT3, interval: Math.max(1, Math.floor(dates.length / 8)) }, axisTick: { show: false } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: 'rgba(35,43,58,.55)' } }, axisLine: { show: false }, axisLabel: { color: TXT3, formatter: (v) => v.toFixed(2) } },
      { gridIndex: 1, splitNumber: 2, splitLine: { show: false }, axisLine: { show: false }, axisLabel: { show: false } },
      { gridIndex: 2, splitNumber: 3, splitLine: { lineStyle: { color: 'rgba(35,43,58,.55)' } }, axisLine: { show: false }, axisLabel: { color: TXT3 } },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: UP, color0: DN, borderColor: UP, borderColor0: DN },
        markPoint: {
          symbolSize: 42, label: { fontSize: 10, color: '#1a0f05', fontWeight: 700 },
          data: [{ name: '首板', coord: [ai, K[ai]?.high ?? 0], value: '首板', itemStyle: { color: ACC } }],
        },
        markArea: {
          silent: true,
          itemStyle: { color: 'rgba(255,138,61,.07)', borderColor: 'rgba(255,138,61,.35)', borderWidth: 1, borderType: 'dashed' },
          label: { show: true, position: 'insideTop', color: ACC, fontSize: 10.5, formatter: '横盘蓄势区' },
          data: [[{ xAxis: dates[Math.max(0, cs[0])] }, { xAxis: dates[Math.min(dates.length - 1, cs[1])] }]],
        },
      },
      { name: 'MA5', type: 'line', data: d.ma5 || [], xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { width: 1.1, color: '#ffd166' } },
      { name: 'MA10', type: 'line', data: d.ma10 || [], xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { width: 1.1, color: '#4a9eff' } },
      { name: 'MA20', type: 'line', data: d.ma20 || [], xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { width: 1.1, color: '#b07cff' } },
      { name: '成交量', type: 'bar', data: vols, xAxisIndex: 1, yAxisIndex: 1 },
      { name: 'DIF', type: 'line', data: dif, xAxisIndex: 2, yAxisIndex: 2, showSymbol: false, lineStyle: { width: 1.2, color: '#ffd166' } },
      { name: 'DEA', type: 'line', data: dea, xAxisIndex: 2, yAxisIndex: 2, showSymbol: false, lineStyle: { width: 1.2, color: '#4a9eff' } },
      { name: 'MACD', type: 'bar', data: hist, xAxisIndex: 2, yAxisIndex: 2, barWidth: '55%' },
    ],
  });
  window.addEventListener('resize', () => chart.resize());
}

/* ─────────────── 事件绑定 ─────────────── */
(function init() {
  loadStats();
  runScan(false);

  $('btnScan').addEventListener('click', () => { state.current = null; runScan(true); });
  $('btnRebuild').addEventListener('click', () => {
    if (!confirm('将从上游重新拉取候选股日线（约 1~2 分钟），确定继续？\n\n（全市场刷新请运行 research/28_refresh_qfq.py）')) return;
    state.current = null; runScan(false, true);
  });

  let t = null;
  const lazy = () => { clearTimeout(t); t = setTimeout(() => runScan(false), 420); };

  $('fGap').addEventListener('input', (e) => { $('fGapVal').textContent = `3 ~ ${e.target.value} 天`; });
  $('fGap').addEventListener('change', lazy);
  $('fAmp').addEventListener('input', (e) => {
    $('fAmpVal').textContent = +e.target.value === 0 ? '不限' : `≤ ${e.target.value}%`;
  });
  $('fAmp').addEventListener('change', lazy);
  $('fVol').addEventListener('input', (e) => { $('fVolVal').textContent = (+e.target.value).toFixed(2); });
  $('fVol').addEventListener('change', lazy);

  $('fSearch').addEventListener('input', applyFilter);
  $('fMarket').querySelectorAll('button').forEach(b =>
    b.addEventListener('click', () => {
      $('fMarket').querySelectorAll('button').forEach(x => x.classList.remove('active'));
      b.classList.add('active'); state.current = null; runScan(false);
    }));
})();
