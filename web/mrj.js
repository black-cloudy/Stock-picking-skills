/* ══════════════════════════════════════════════════════════════
   形态量化选股 · 前端逻辑
   三种模式：经典美人肩 / 广义美人肩 / 低位票
   （ECharts K线 + MACD + 成交量）
   ══════════════════════════════════════════════════════════════ */
'use strict';

const UP = '#ff4d3d', DN = '#00c076', TXT2 = '#9aa5b8', TXT3 = '#66718a',
      LINE = '#232b3a', ACC = '#ff8a3d';

const state = {
  mode: 'classic',
  modes: {},            // /api/mrj/stats 返回的三模式元信息
  items: [],
  filtered: [],
  current: null,
  chart: null,
  loading: false,
};

const $ = (id) => document.getElementById(id);
const f2 = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '--' : Number(v).toFixed(d);
const cls = (v) => v > 0 ? 'up' : (v < 0 ? 'dn' : 'flat');
const sgn = (v) => (v > 0 ? '+' : '') + f2(v);
const meta = () => state.modes[state.mode] || {};
const preset = () => meta().params || {};

function toast(msg, ms = 2200) {
  const t = $('toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, ms);
}

/* ─────────────── 模式元信息（统计条 / 模型卡 / 引擎标题） ─────────────── */
async function loadStats() {
  try {
    const r = await fetch('/api/mrj/stats').then(r => r.json());
    state.modes = r.modes || {};
    if (r.default) state.mode = r.default;
  } catch (e) { console.warn(e); }
}

function applyMode(mode) {
  state.mode = mode;
  state.current = null;
  state.items = [];
  state.filtered = [];

  document.querySelectorAll('#modes .mode')
    .forEach(b => b.classList.toggle('active', b.dataset.mode === mode));

  const m = meta(), p = m.params || {};
  const isLp = mode === 'lowpos';

  // 过滤器切换
  $('fgMrj').hidden = isLp;
  $('fgLowpos').hidden = !isLp;

  // 统计条
  $('sbTag').textContent = `${m.label || mode} · 运行中`;
  $('sbBacktest').innerHTML = m.backtest_text || '五年实测：--';
  $('sbWindow').innerHTML = (m.window || '').replace(/^最佳潜伏窗口：?/, '最佳潜伏窗口：') || '--';

  // 模型卡
  $('mcTitle').textContent = isLp
    ? '模型核心价值：暴跌见底 → 低位均线粘合 → 拐头 / 放量突破'
    : (mode === 'broad'
      ? '模型核心价值：大涨蓄势 → 横盘不回落 → 等一根涨停'
      : '模型核心价值：涨停启动 → 缩量横盘蓄势 → 二次涨停爆发');
  $('mcText').innerHTML = m.desc || '';
  $('mcMetrics').innerHTML = (m.metrics || [])
    .map(([v, l]) => `<div class="m"><i>${v}</i><span>${l}</span></div>`).join('');

  // 引擎标题
  $('ehTitle').textContent = m.engine || '';
  $('ehCond').textContent = m.condition || '';
  $('listTitle').textContent = isLp ? '低位粘合候选标的' : '符合形态候选标的';

  // 过滤器回落到该模式预设
  if (isLp) {
    $('fPos').value = p.max_position ?? 65;
    $('fVolLp').value = 0;
    syncLowposLabels();
  } else {
    const lo = p.min_gap ?? 3;
    $('fGap').min = lo;
    $('fGap').value = Math.max(lo, p.max_gap ?? 8);
    $('fAmp').value = p.max_amp ?? 4;
    $('fVol').value = p.min_vol ?? 0.9;
    syncMrjLabels();
  }

  $('detail').innerHTML = `<div class="empty big">← 从左侧选择一只标的查看详情</div>`;
  $('chipCount').textContent = '候选 --';
  $('chipTime').textContent = '--';
  $('cards').innerHTML = `<div class="empty">正在扫描…</div>`;
}

function syncMrjLabels() {
  const lo = +(preset().min_gap ?? 3);
  $('fGapVal').textContent = `${lo} ~ ${$('fGap').value} 天`;
  const a = +$('fAmp').value;
  $('fAmpVal').textContent = a === 0 ? '不限' : `≤ ${a}%`;
  $('fVolVal').textContent = (+$('fVol').value).toFixed(2);
}

function syncLowposLabels() {
  $('fPosVal').textContent = `≤ ${$('fPos').value}%`;
  const v = +$('fVolLp').value;
  $('fVolLpVal').textContent = v === 0 ? '不限' : `≥ ${v.toFixed(1)}`;
}

/* ─────────────── 扫描 ─────────────── */
function scanParams() {
  const market = document.querySelector('#fMarket button.active')?.dataset.m || 'main';
  if (state.mode === 'lowpos') {
    return {
      mode: 'lowpos',
      market,
      sub: document.querySelector('#fSub button.active')?.dataset.s || 'both',
      max_pos: +$('fPos').value,
      min_vol: +$('fVolLp').value || null,
      limit: 60,
    };
  }
  return {
    mode: state.mode,
    market,
    max_gap: +$('fGap').value,
    max_amp: +$('fAmp').value,
    min_vol: +$('fVol').value,
    limit: 60,
  };
}

async function runScan(refresh = false, forceBars = false) {
  if (state.loading) return;
  state.loading = true;
  const label = meta().label || '形态';
  $('cards').innerHTML = `<div class="empty"><span class="spin">◌</span> ` +
    (forceBars ? '正在从上游把日线更新到最新交易日…（当天首次约 1~2 分钟）'
               : `正在更新今日行情并扫描 · ${label}…` +
                 (state.mode === 'lowpos' ? '（当天首次约 1~2 分钟）' : '')) + `</div>`;
  $('chipTime').textContent = '扫描中…';

  const p = scanParams();
  const clean = {};
  Object.keys(p).forEach(k => { if (p[k] !== null && p[k] !== undefined && p[k] !== '') clean[k] = p[k]; });
  const qs = new URLSearchParams({ ...clean, refresh: refresh ? 1 : 0, force_bars: forceBars ? 1 : 0 });

  try {
    const r = await fetch('/api/mrj/scan?' + qs).then(r => r.json());
    if (!r.ok) throw new Error(r.detail || '扫描失败');
    state.items = r.items || [];
    const stale = !!r.is_stale;
    const cov = r.coverage || {};
    const covTxt = cov.total ? `（${(cov.covered || 0).toLocaleString()}/${(cov.total || 0).toLocaleString()} 只已到该日，${cov.pct || 0}%）` : '';
    $('sbDate').innerHTML = `数据日期：${r.trade_date || '--'}${covTxt}` +
      (stale ? ` <b style="color:var(--up)">← 上游已到 ${r.upstream_date}，点「⟳ 刷新日线」补齐</b>` : '');
    if (stale) toast(`⚠ 日线没跟上：本地多数停在 ${r.trade_date || '?'}（覆盖 ${cov.pct || 0}%），上游已到 ${r.upstream_date}。点「⟳ 刷新日线」补齐`, 7000);
    $('chipCount').textContent = `候选 ${r.total} 只`;
    $('chipTime').textContent = `${r.cached ? '缓存' : '耗时 ' + r.elapsed + 's'}`;
    applyFilter();
    if (state.filtered.length) selectStock(state.filtered[0].code);
    else if (r.hint) $('cards').innerHTML = `<div class="empty">${r.hint}</div>`;
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
  $('listHint').textContent = `共 ${state.filtered.length} 只 · 按匹配得分排序`;
  renderCards();
}

function fallbackMetrics(it) {
  if (state.mode === 'lowpos') {
    return [
      { value: f2(it.close_pos, 0) + '%', label: '250日位置' },
      { value: f2(it.avg_amp) + '%', label: '均线离散度' },
      { value: it.gap ?? '--', label: '粘合天数' },
      { value: f2(it.score, 1), label: '匹配得分' },
    ];
  }
  return [
    { value: it.gap ?? '--', label: '蓄势天数' },
    { value: f2(it.avg_amp) + '%', label: '日均振幅' },
    { value: f2(it.vol_trend), label: '量能趋势' },
    { value: f2(it.score, 1), label: '匹配得分' },
  ];
}

function renderCards() {
  const box = $('cards');
  if (!state.filtered.length) {
    box.innerHTML = `<div class="empty">没有符合条件的标的，试试放宽过滤条件</div>`;
    return;
  }
  box.innerHTML = state.filtered.map(it => {
    const c = cls(it.change_pct || 0);
    const strong = it.status === '二板已成' || it.status === '放量突破';
    const metrics = (it.metrics && it.metrics.length ? it.metrics : fallbackMetrics(it))
      .map(m => `<div class="s"><i>${m.value}</i><span>${m.label}</span></div>`).join('');
    return `
    <div class="card${strong ? ' lu' : ''}${state.current === it.code ? ' active' : ''}" data-code="${it.code}">
      <div class="c-top">
        <div>
          <div>
            <span class="c-code">${it.code}</span>
            <span class="c-name">${it.name || ''}</span>
            <span class="c-badge${strong ? '' : ' warn'}">${it.status || ''}</span>
          </div>
          <div class="c-ind">${it.industry || '—'}</div>
        </div>
        <div class="c-price">
          <div class="p ${c}">${f2(it.price)}</div>
          <span class="pc ${c}">${sgn(it.change_pct)}%</span>
        </div>
      </div>
      <div class="c-stats">${metrics}</div>
      <div class="c-note">${it.note || ''}</div>
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

  const p = { code, mode: state.mode, bars: 90 };
  if (state.mode === 'lowpos') {
    p.sub = document.querySelector('#fSub button.active')?.dataset.s || 'both';
    p.max_pos = +$('fPos').value;
  } else {
    p.close_band = preset().close_band ?? 0.035;
  }
  try {
    const r = await fetch('/api/mrj/detail?' + new URLSearchParams(p)).then(r => r.json());
    if (!r.ok) throw new Error(r.detail || '加载失败');
    renderDetail(r);
  } catch (e) {
    panel.innerHTML = `<div class="empty big">${e.message}</div>`;
  }
}

const PLAN_TITLE = { classic: '经典美人肩实战潜伏作战手册', broad: '广义美人肩实战潜伏作战手册', lowpos: '低位票实战操作手册' };
const CHECK_TITLE = { classic: '量化特征契合度全息体检', broad: '量化特征契合度全息体检', lowpos: '低位粘合特征全息体检' };

function renderDetail(d) {
  const head = d.head || {}, plan = d.plan || {};
  const stats = d.stats || [];
  const dstr = stats[0]?.value || '';
  const mode = d.mode || state.mode;

  $('detail').innerHTML = `
    <div class="d-head">
      <div class="d-title">
        <span class="code">${d.code}</span>
        <span class="nm">${d.name || ''}</span>
        <span class="sub">${head.sub || ''}</span>
      </div>
      <div class="d-ma">
        ${(head.chips || []).map(c => `<span>${c.label} <b>${c.value}</b></span>`).join('')}
      </div>
    </div>

    <div class="stat-row">
      ${stats.map(s => `<div class="s"><span>${s.label}</span><i class="${s.tone || ''}">${s.value}</i></div>`).join('')}
    </div>

    <div class="chart" id="chart"></div>

    <div class="d-note">
      <b>形态节点诊断（${dstr}）：</b>
      ${d.note || ''}
    </div>

    <div class="d-sec">
      <h3>${PLAN_TITLE[mode] || '实战作战手册'}</h3>
      <div class="plan-grid">
        ${(plan.items || []).map(it => `
          <div class="p ${it.kind}"><span>${it.label}</span><i>${it.value}</i><em>${it.hint || ''}</em></div>
        `).join('')}
      </div>
    </div>

    <div class="d-sec">
      <h3>${CHECK_TITLE[mode] || '量化特征体检'}</h3>
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
  const vols = K.map((x) => ({
    value: x.volume,
    itemStyle: { color: (x.close >= x.open) ? 'rgba(255,77,61,.55)' : 'rgba(0,192,118,.55)' },
  }));
  const dif = (d.dif || []).map(v => v === null ? '-' : +Number(v).toFixed(3));
  const dea = (d.dea || []).map(v => v === null ? '-' : +Number(v).toFixed(3));
  const hist = (d.hist || []).map(v => ({
    value: v === null ? '-' : +Number(v).toFixed(3),
    itemStyle: { color: (v >= 0 ? 'rgba(255,77,61,.75)' : 'rgba(0,192,118,.75)') },
  }));

  const mk = d.marks || {};
  const pt = mk.point || null;
  const ar = mk.area || null;
  const pointData = (pt && pt.index >= 0 && pt.index < K.length)
    ? [{
        name: pt.label, value: pt.label,
        coord: [dates[pt.index], K[pt.index]?.high ?? 0],
        itemStyle: { color: ACC },
      }]
    : [];
  const areaData = (ar && K.length)
    ? [[
        { xAxis: dates[Math.max(0, Math.min(dates.length - 1, ar.start))] },
        { xAxis: dates[Math.max(0, Math.min(dates.length - 1, ar.end))] },
      ]]
    : [];

  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    textStyle: { color: TXT2, fontSize: 11 },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross', lineStyle: { color: '#3a465c' } },
      backgroundColor: 'rgba(20,25,36,.96)', borderColor: LINE, textStyle: { color: '#e7ecf5', fontSize: 11.5 },
      formatter: (ps) => {
        const i = ps[0].dataIndex, k = K[i];
        if (!k) return '';
        return `<b>${k.date}</b><br/>开 ${f2(k.open)} 收 ${f2(k.close)}<br/>低 ${f2(k.low)} 高 ${f2(k.high)}<br/>` +
               `量 ${((k.volume || 0) / 1e4).toFixed(1)}万手`;
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
          data: pointData,
        },
        markArea: {
          silent: true,
          itemStyle: { color: 'rgba(255,138,61,.07)', borderColor: 'rgba(255,138,61,.35)', borderWidth: 1, borderType: 'dashed' },
          label: { show: true, position: 'insideTop', color: ACC, fontSize: 10.5, formatter: (ar && ar.label) || '' },
          data: areaData,
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
  (async () => {
    await loadStats();
    const qm = new URLSearchParams(location.search).get('mode');
    if (qm && state.modes[qm]) state.mode = qm;
    applyMode(state.mode);
    runScan(false);
  })();

  // 模式切换
  document.querySelectorAll('#modes .mode').forEach(b =>
    b.addEventListener('click', () => {
      if (state.loading || b.dataset.mode === state.mode) return;
      applyMode(b.dataset.mode);
      try { history.replaceState(null, '', '?mode=' + b.dataset.mode); } catch (e) { /* ignore */ }
      runScan(false);
    }));

  $('btnScan').addEventListener('click', () => { state.current = null; runScan(true); });
  $('btnRebuild').addEventListener('click', () => {
    if (!confirm('将把日线增量更新到最新交易日（只补落后的股票，当天首次约 1~2 分钟），确定继续？')) return;
    state.current = null; runScan(false, true);
  });

  let t = null;
  const lazy = () => { clearTimeout(t); t = setTimeout(() => runScan(false), 420); };

  // 美人肩过滤器
  ['fGap', 'fAmp', 'fVol'].forEach(id => {
    $(id).addEventListener('input', syncMrjLabels);
    $(id).addEventListener('change', lazy);
  });

  // 低位票过滤器
  ['fPos', 'fVolLp'].forEach(id => {
    $(id).addEventListener('input', syncLowposLabels);
    $(id).addEventListener('change', lazy);
  });
  $('fSub').querySelectorAll('button').forEach(b =>
    b.addEventListener('click', () => {
      $('fSub').querySelectorAll('button').forEach(x => x.classList.remove('active'));
      b.classList.add('active'); state.current = null; runScan(false);
    }));

  $('fSearch').addEventListener('input', applyFilter);
  $('fMarket').querySelectorAll('button').forEach(b =>
    b.addEventListener('click', () => {
      $('fMarket').querySelectorAll('button').forEach(x => x.classList.remove('active'));
      b.classList.add('active'); state.current = null; runScan(false);
    }));
})();
