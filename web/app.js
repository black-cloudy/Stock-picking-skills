/* ============================================================================
 * StockAgent · 前端应用
 * 功能：大盘指数、股票搜索、自选股、实时行情、五档盘口、逐笔成交、
 *       分时图 / 五日分时 / 1·5·15·30·60 分钟 K / 日K / 周K / 月K
 * ==========================================================================*/

(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const LS_WATCH = 'stockagent.watchlist';
  const LS_CURRENT = 'stockagent.current';

  const QUOTE_MS = 3000;    // 行情刷新间隔
  const MINUTE_MS = 15000;  // 分时刷新间隔
  const KLINE_MS = 30000;   // K 线刷新间隔
  const INDEX_MS = 10000;   // 指数刷新间隔

  const COLOR = { up: '#f0413e', down: '#12b886', flat: '#9aa7b8', ma5: '#f2b93b', ma10: '#4a9eff', ma20: '#c678dd', ma60: '#56d4dd' };

  // ─────────────────────────── 状态 ───────────────────────────

  const state = {
    current: null,          // {code, name}
    period: 'minute',
    adjust: 'qfq',
    watch: loadWatch(),
    quotes: {},
    book: null,
    chart: null,
    rankTimer: 0,
    autoRefresh: true,
    trading: false,
    searchItems: [],
    searchCursor: -1,
    timer: null,
  };

  // ─────────────────────────── 工具函数 ───────────────────────────

  function loadWatch() {
    try {
      const raw = JSON.parse(localStorage.getItem(LS_WATCH) || 'null');
      if (Array.isArray(raw) && raw.length) return raw;
    } catch (e) { /* ignore */ }
    return ['600519', '000001', '300750', '601318', '000858'];
  }
  function saveWatch() { localStorage.setItem(LS_WATCH, JSON.stringify(state.watch)); }

  const n = (v) => (v === null || v === undefined || v === '' || isNaN(v) ? null : Number(v));
  const fmt = (v, d = 2) => (n(v) === null ? '--' : n(v).toFixed(d));
  const sign = (v) => (n(v) === null ? '' : n(v) > 0 ? '+' : '');
  const pct = (v) => (n(v) === null ? '--' : sign(v) + n(v).toFixed(2) + '%');
  const dirClass = (v) => (n(v) === null ? 'flat' : n(v) > 0 ? 'up' : n(v) < 0 ? 'down' : 'flat');

  function money(v) {
    const x = n(v);
    if (x === null) return '--';
    const a = Math.abs(x);
    if (a >= 1e12) return (x / 1e12).toFixed(2) + '万亿';
    if (a >= 1e8) return (x / 1e8).toFixed(2) + '亿';
    if (a >= 1e4) return (x / 1e4).toFixed(2) + '万';
    return x.toFixed(0);
  }
  function hand(v) {
    const x = n(v);
    if (x === null) return '--';
    if (Math.abs(x) >= 1e4) return (x / 1e4).toFixed(2) + '万手';
    return x.toFixed(0) + '手';
  }

  async function api(path) {
    const resp = await fetch('/api' + path, { headers: { Accept: 'application/json' } });
    let body = null;
    try { body = await resp.json(); } catch (e) { /* ignore */ }
    if (!resp.ok) throw new Error((body && body.detail) || ('HTTP ' + resp.status));
    return body;
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $('toast');
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
  }

  // 静态资源用 ECharts
  let chart = null;
  function ensureChart() {
    if (!chart) {
      chart = echarts.init($('chart'), null, { renderer: 'canvas' });
      window.addEventListener('resize', () => chart.resize());
    }
    return chart;
  }

  // ─────────────────────────── 大盘指数 ───────────────────────────

  async function loadIndices() {
    try {
      const data = await api('/market/indices');
      const box = $('indices');
      box.innerHTML = (data.items || []).map((it) => {
        const c = dirClass(it.change_pct);
        return `<div class="idx-item">
          <span class="idx-name">${it.index_name || it.name}</span>
          <span class="idx-row">
            <span class="idx-value ${c}">${fmt(it.price)}</span>
            <span class="idx-pct ${c}">${sign(it.change)}${fmt(it.change)} ${pct(it.change_pct)}</span>
          </span>
        </div>`;
      }).join('');
    } catch (e) {
      $('indices').innerHTML = '<div class="idx-placeholder">指数加载失败</div>';
    }
  }

  async function loadStatus() {
    try {
      const st = await api('/market/status');
      const el = $('marketStatus');
      el.textContent = st.label;
      el.className = 'market-status' + (st.trading ? ' live' : '');
      state.trading = !!st.trading;
    } catch (e) { /* ignore */ }
  }

  // ─────────────────────────── 搜索 ───────────────────────────

  const searchInput = $('searchInput');
  const searchBox = $('searchResults');
  let searchTimer = null;

  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = searchInput.value.trim();
    if (!q) { searchBox.hidden = true; return; }
    searchTimer = setTimeout(() => doSearch(q), 220);
  });
  searchInput.addEventListener('keydown', (e) => {
    if (searchBox.hidden) return;
    if (e.key === 'ArrowDown') { state.searchCursor = Math.min(state.searchCursor + 1, state.searchItems.length - 1); paintSearch(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { state.searchCursor = Math.max(state.searchCursor - 1, 0); paintSearch(); e.preventDefault(); }
    else if (e.key === 'Enter') {
      const item = state.searchItems[state.searchCursor >= 0 ? state.searchCursor : 0];
      if (item) { selectStock(item.code, item.name); searchInput.value = ''; searchBox.hidden = true; }
    } else if (e.key === 'Escape') { searchBox.hidden = true; }
  });
  document.addEventListener('click', (e) => {
    if (!searchBox.contains(e.target) && e.target !== searchInput) searchBox.hidden = true;
  });

  async function doSearch(q) {
    try {
      const data = await api('/search?q=' + encodeURIComponent(q) + '&limit=12');
      state.searchItems = data.items || [];
      state.searchCursor = state.searchItems.length ? 0 : -1;
      if (!state.searchItems.length) { searchBox.hidden = true; return; }
      paintSearch();
      searchBox.hidden = false;
    } catch (e) { searchBox.hidden = true; }
  }

  function paintSearch() {
    searchBox.innerHTML = state.searchItems.map((it, i) => `
      <div class="search-item${i === state.searchCursor ? ' active' : ''}" data-code="${it.code}" data-name="${it.name}">
        <span class="si-name">${it.name}</span>
        <span class="si-code">${it.code} · ${it.type || ''}</span>
      </div>`).join('');
    searchBox.querySelectorAll('.search-item').forEach((el) => {
      el.addEventListener('click', () => {
        selectStock(el.dataset.code, el.dataset.name);
        searchInput.value = ''; searchBox.hidden = true;
      });
    });
  }

  // ─────────────────────────── 自选股 / 排行 ───────────────────────────

  function renderWatch() {
    const box = $('watchList');
    if (!state.watch.length) {
      box.innerHTML = '<div class="empty-tip">还没有自选股<br>搜索后点击「加入自选」</div>';
      return;
    }
    box.innerHTML = state.watch.map((code) => {
      const q = state.quotes[code];
      const c = q ? dirClass(q.change_pct) : 'flat';
      const active = state.current && state.current.code === code ? ' active' : '';
      return `<div class="watch-row${active}" data-code="${code}">
        <span>
          <div class="wr-name">${q ? q.name : code}</div>
          <div class="wr-code">${code}</div>
        </span>
        <span class="wr-price ${c}">${q ? fmt(q.price) : '--'}</span>
        <span class="wr-pct ${c}">${q ? pct(q.change_pct) : '--'}</span>
        <span class="wr-del" title="移出自选">✕</span>
      </div>`;
    }).join('');
    box.querySelectorAll('.watch-row').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.target.classList.contains('wr-del')) {
          state.watch = state.watch.filter((c) => c !== el.dataset.code);
          saveWatch(); renderWatch(); return;
        }
        selectStock(el.dataset.code);
      });
      el.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        state.watch = state.watch.filter((c) => c !== el.dataset.code);
        saveWatch(); renderWatch();
      });
    });
  }

  async function refreshWatchQuotes() {
    if (!state.watch.length) { renderWatch(); return; }
    try {
      const data = await api('/batch/quotes?codes=' + state.watch.join(','));
      (data.items || []).forEach((q) => { state.quotes[q.code] = q; });
      updateWatchDom();
    } catch (e) { /* ignore */ }
  }

  function updateWatchDom() {
    document.querySelectorAll('#watchList .watch-row').forEach((el) => {
      const q = state.quotes[el.dataset.code];
      if (!q) return;
      const c = dirClass(q.change_pct);
      const price = el.querySelector('.wr-price');
      const pc = el.querySelector('.wr-pct');
      const nm = el.querySelector('.wr-name');
      price.textContent = fmt(q.price); price.className = 'wr-price ' + c;
      pc.textContent = pct(q.change_pct); pc.className = 'wr-pct ' + c;
      nm.textContent = q.name || nm.textContent;
    });
  }

  async function loadRank() {
    const market = $('rankMarket').value;
    const sort = $('rankSort').value;
    try {
      const data = await api(`/stocks?market=${market}&sort_field=${sort}&order=desc&page_size=30`);
      $('rankList').innerHTML = (data.items || []).map((q) => {
        const c = dirClass(q.change_pct);
        return `<div class="watch-row" data-code="${q.code}">
          <span><div class="wr-name">${q.name}</div><div class="wr-code">${q.code}</div></span>
          <span class="wr-price ${c}">${fmt(q.price)}</span>
          <span class="wr-pct ${c}">${pct(q.change_pct)}</span>
          <span class="wr-del" title="加入自选">＋</span>
        </div>`;
      }).join('');
      $('rankList').querySelectorAll('.watch-row').forEach((el) => {
        el.addEventListener('click', (e) => {
          if (e.target.classList.contains('wr-del')) {
            const code = el.dataset.code;
            if (state.watch.includes(code)) toast('已在自选列表中');
            else { state.watch.unshift(code); saveWatch(); renderWatch(); refreshWatchQuotes(); toast('已加入自选'); }
            return;
          }
          selectStock(el.dataset.code);
        });
      });
    } catch (e) {
      $('rankList').innerHTML = '<div class="empty-tip">排行榜加载失败</div>';
    }
  }

  // 侧栏 tab 切换
  document.querySelectorAll('.side-tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.side-tab').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      const tab = btn.dataset.tab;
      $('panelWatch').classList.toggle('hidden', tab !== 'watch');
      $('panelRank').classList.toggle('hidden', tab !== 'rank');
      if (tab === 'rank') loadRank();
    });
  });
  $('rankMarket').addEventListener('change', loadRank);
  $('rankSort').addEventListener('change', loadRank);

  $('addWatchBtn').addEventListener('click', () => {
    if (!state.current) { toast('请先选择一只股票'); return; }
    const code = state.current.code;
    if (state.watch.includes(code)) { toast('已在自选列表中'); return; }
    state.watch.unshift(code);
    saveWatch(); renderWatch(); refreshWatchQuotes();
    toast('已加入自选：' + (state.current.name || code));
  });

  // ─────────────────────────── 选中股票 ───────────────────────────

  async function selectStock(code, name) {
    state.current = { code, name: name || code };
    localStorage.setItem(LS_CURRENT, JSON.stringify(state.current));
    document.querySelectorAll('.watch-row').forEach((el) => {
      el.classList.toggle('active', el.dataset.code === code);
    });
    $('stkName').textContent = name || code;
    $('stkCode').textContent = code;
    $('chartLoading').hidden = false;

    await Promise.all([refreshQuote(), loadChart()]);
    loadAux();
    refreshWatchQuotes();
  }

  async function refreshQuote() {
    if (!state.current) return;
    try {
      const q = await api(`/stock/${state.current.code}/quote?orderbook=true`);
      state.quotes[q.code] = q;
      state.book = q.orderbook || null;
      renderHead(q);
      renderOrderbook(q.orderbook);
      renderMetrics(q);
      updateWatchDom();
    } catch (e) {
      // 静默
    }
  }

  async function loadAux() {
    if (!state.current) return;
    try {
      const data = await api(`/stock/${state.current.code}/ticks?limit=40`);
      renderTicks((data.items || []).slice().reverse());
    } catch (e) { $('ticks').innerHTML = '<div class="empty-tip">暂无数据</div>'; }
  }

  // ─────────────────────────── 头部行情 ───────────────────────────

  function renderHead(q) {
    state.quotes[q.code] = q;
    const c = dirClass(q.change_pct);
    $('stkName').textContent = q.name || q.code;
    $('stkCode').textContent = q.code + ' · ' + (q.market_name || '');
    $('stkIndustry').textContent = q.industry || '—';
    $('stkIndustry').style.display = q.industry ? '' : 'none';

    const price = $('stkPrice');
    price.textContent = fmt(q.price, q.decimal || 2);
    price.className = 'big-price ' + c;

    const chg = $('stkChange');
    chg.textContent = sign(q.change) + fmt(q.change);
    chg.className = 'big-chg ' + c;

    const p = $('stkPct');
    p.textContent = pct(q.change_pct);
    p.className = 'big-pct ' + c;

    $('stkMeta').innerHTML = [
      ['今开', fmt(q.open), dirClass(n(q.open) - n(q.pre_close))],
      ['昨收', fmt(q.pre_close), ''],
      ['最高', fmt(q.high), dirClass(n(q.high) - n(q.pre_close))],
      ['最低', fmt(q.low), dirClass(n(q.low) - n(q.pre_close))],
      ['成交量', hand(q.volume), ''],
      ['成交额', money(q.amount), ''],
      ['换手', q.turnover_rate != null ? fmt(q.turnover_rate) + '%' : '--', ''],
      ['振幅', q.amplitude != null ? fmt(q.amplitude) + '%' : '--', ''],
    ].map(([k, v, cc]) => `<span>${k} <b class="${cc}">${v}</b></span>`).join('');

    document.title = `${q.name} ${fmt(q.price)} ${pct(q.change_pct)} · StockAgent`;
  }

  function renderMetrics(q) {
    const rows = [
      ['均价', fmt(q.avg_price)],
      ['量比', fmt(q.volume_ratio)],
      ['涨停', fmt(q.limit_up)],
      ['跌停', fmt(q.limit_down)],
      ['市盈率(动)', fmt(q.pe_dynamic)],
      ['市净率', fmt(q.pb)],
      ['总市值', money(q.total_mv)],
      ['流通市值', money(q.float_mv)],
      ['委比', q.bid_ratio != null ? fmt(q.bid_ratio) + '%' : '--'],
      ['委差', fmt(q.bid_diff, 0)],
      ['每股净资产', fmt(q.eps)],
      ['主力净流入', money(q.main_inflow)],
    ];
    $('metrics').innerHTML = rows.map(([k, v]) =>
      `<div class="metric"><span class="m-k">${k}</span><span class="m-v">${v}</span></div>`).join('');
  }

  // ─────────────────────────── 五档盘口 ───────────────────────────

  function renderOrderbook(book) {
    const box = $('orderbook');
    if (!book || !book.asks || !book.bids) {
      box.innerHTML = '<div class="empty-tip">暂无盘口数据</div>';
      return;
    }
    const maxVol = Math.max(
      1,
      ...book.asks.map((a) => n(a.volume) || 0),
      ...book.bids.map((b) => n(b.volume) || 0),
    );
    const base = n(book.pre_close) || n(book.price) || 0;
    const row = (label, item, side) => {
      const p = n(item.price);
      const v = n(item.volume) || 0;
      const cc = p == null || !base ? 'flat' : p > base ? 'up' : p < base ? 'down' : 'flat';
      const w = Math.round((v / maxVol) * 100);
      const bg = side === 'ask' ? 'rgba(240,65,62,.9)' : 'rgba(18,184,134,.9)';
      return `<div class="ob-row">
        <span class="ob-label">${label}</span>
        <span class="ob-bar" style="width:${w}%;background:${bg}"></span>
        <span class="ob-price ${cc}">${p == null ? '--' : fmt(p)}</span>
        <span class="ob-vol">${v ? v.toFixed(0) : '--'}</span>
      </div>`;
    };
    const asks = book.asks.slice().reverse().map((a, i) => row('卖' + (5 - i), a, 'ask')).join('');
    const bids = book.bids.map((b, i) => row('买' + (i + 1), b, 'bid')).join('');
    box.innerHTML = asks + '<div class="ob-sep"></div>' + bids +
      `<div class="ob-mid"><span>最新 ${fmt(book.price)}</span><span>${book.time || ''}</span></div>`;
  }

  // ─────────────────────────── 逐笔成交 ───────────────────────────

  function renderTicks(items) {
    const box = $('ticks');
    if (!items || !items.length) { box.innerHTML = '<div class="empty-tip">暂无成交明细</div>'; return; }
    box.innerHTML = items.map((t) => {
      const cc = t.side === 'buy' ? 'up' : t.side === 'sell' ? 'down' : 'flat';
      return `<div class="tick-row">
        <span class="t-time">${(t.time || '').slice(0, 8)}</span>
        <span class="t-price ${cc}">${fmt(t.price)}</span>
        <span class="t-vol">${fmt(t.volume, 0)}</span>
        <span class="t-amt">${money(t.amount)}</span>
      </div>`;
    }).join('');
  }

  // ─────────────────────────── 图表 ───────────────────────────

  async function loadChart() {
    if (!state.current) return;
    const period = state.period;
    $('chartLoading').hidden = false;
    try {
      if (period === 'minute' || period === 'minute5') {
        const days = period === 'minute5' ? 5 : 1;
        const data = await api(`/stock/${state.current.code}/minute?days=${days}`);
        drawMinute(data);
      } else {
        const limit = period === 'daily' ? 500 : period === 'weekly' ? 300 : period === 'monthly' ? 200 : 400;
        const data = await api(`/stock/${state.current.code}/kline?period=${period}&adjust=${state.adjust}&limit=${limit}`);
        drawKline(data);
      }
    } catch (e) {
      toast('图表加载失败：' + e.message);
    } finally {
      $('chartLoading').hidden = true;
    }
  }

  function timeLabel(v) {
    if (!v) return '';
    if (state.period === 'minute5') return v.slice(5, 16);   // MM-DD HH:MM
    return v.slice(11, 16);                                   // HH:MM
  }

  function drawMinute(data) {
    const c = ensureChart();
    const trends = data.trends || [];
    if (!trends.length) { c.clear(); toast('暂无分时数据'); return; }

    const pre = n(data.pre_close) || 0;
    const times = trends.map((t) => t.time);
    const prices = trends.map((t) => n(t.price));
    const avgs = trends.map((t) => n(t.avg_price));
    const vols = trends.map((t) => n(t.volume) || 0);

    let maxDev = 0;
    trends.forEach((t) => {
      const p = n(t.price), a = n(t.avg_price);
      if (p != null) maxDev = Math.max(maxDev, Math.abs(p - pre));
      if (a != null) maxDev = Math.max(maxDev, Math.abs(a - pre));
    });
    if (!isFinite(maxDev) || maxDev <= 0) maxDev = pre * 0.01 || 1;
    maxDev = Math.max(maxDev * 1.15, pre * 0.003);
    const yMin = pre - maxDev, yMax = pre + maxDev;
    const toPct = (v) => (pre ? ((v - pre) / pre) * 100 : 0);

    const barData = prices.map((p, i) => {
      const prev = i > 0 ? prices[i - 1] : p;
      const rising = (p != null && prev != null) ? p >= prev : true;
      return {
        value: vols[i],
        itemStyle: { color: rising ? 'rgba(240,65,62,.65)' : 'rgba(18,184,134,.65)' },
      };
    });

    const interval = Math.max(1, Math.floor(times.length / 6));

    c.setOption({
      animation: false,
      backgroundColor: 'transparent',
      textStyle: { fontFamily: 'Consolas, monospace' },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', label: { backgroundColor: '#2b3a4f' } },
        backgroundColor: 'rgba(24,34,49,.96)',
        borderColor: '#233044',
        textStyle: { color: '#d7e1ee', fontSize: 12 },
        formatter: (ps) => {
          if (!ps || !ps.length) return '';
          const i = ps[0].dataIndex;
          const t = trends[i];
          const cc = t.change_pct > 0 ? COLOR.up : t.change_pct < 0 ? COLOR.down : COLOR.flat;
          return `${t.time}<br/>价格 <b style="color:${cc}">${fmt(t.price)}</b>` +
            `　涨跌 <b style="color:${cc}">${pct(t.change_pct)}</b><br/>` +
            `均价 ${fmt(t.avg_price)}　成交量 ${hand(t.volume)}<br/>成交额 ${money(t.amount)}`;
        },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: 62, right: 66, top: 16, height: '58%' },
        { left: 62, right: 66, top: '76%', height: '17%' },
      ],
      xAxis: [
        {
          type: 'category', data: times, boundaryGap: false,
          axisLine: { lineStyle: { color: '#233044' } }, axisTick: { show: false },
          axisLabel: { color: '#5a6b80', fontSize: 10, interval, formatter: timeLabel },
          splitLine: { show: false },
        },
        {
          type: 'category', data: times, gridIndex: 1, boundaryGap: false,
          axisLine: { lineStyle: { color: '#233044' } }, axisTick: { show: false },
          axisLabel: { show: false },
        },
      ],
      yAxis: [
        {
          gridIndex: 0, min: yMin, max: yMax, splitNumber: 5,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#7d8fa6', fontSize: 10, formatter: (v) => fmt(v) },
          splitLine: { lineStyle: { color: '#1b2634' } },
        },
        {
          gridIndex: 0, min: yMin, max: yMax, splitNumber: 5, position: 'right',
          axisLine: { show: false }, axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: {
            fontSize: 10,
            formatter: (v) => {
              const val = toPct(v);
              const k = val > 0.001 ? 'up' : val < -0.001 ? 'down' : 'flat';
              return `{${k}|${val > 0 ? '+' : ''}${val.toFixed(2)}%}`;
            },
            rich: {
              up: { color: COLOR.up }, down: { color: COLOR.down }, flat: { color: COLOR.flat },
            },
          },
        },
        {
          gridIndex: 1, splitNumber: 2,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#5a6b80', fontSize: 9, formatter: (v) => (v >= 10000 ? (v / 10000).toFixed(0) + '万' : v) },
          splitLine: { lineStyle: { color: '#1b2634' } },
        },
      ],
      series: [
        {
          name: '价格', type: 'line', data: prices, showSymbol: false, symbol: 'none',
          lineStyle: { width: 1.4, color: '#4a9eff' }, itemStyle: { color: '#4a9eff' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(74,158,255,.32)' },
              { offset: 1, color: 'rgba(74,158,255,.01)' },
            ]),
          },
          markLine: {
            silent: true, symbol: 'none',
            data: [{ yAxis: pre }],
            lineStyle: { color: '#5a6b80', type: 'dashed', width: 1 },
            label: { show: false },
          },
          z: 3,
        },
        {
          name: '均价', type: 'line', data: avgs, showSymbol: false, symbol: 'none',
          lineStyle: { width: 1, color: COLOR.ma5 }, itemStyle: { color: COLOR.ma5 }, z: 2,
        },
        {
          name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 2,
          data: barData, barWidth: '55%',
        },
      ],
    }, true);
  }

  function movingAverage(values, period) {
    const out = [];
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
      sum += values[i];
      if (i >= period) sum -= values[i - period];
      out.push(i >= period - 1 ? +(sum / period).toFixed(3) : null);
    }
    return out;
  }

  function drawKline(data) {
    const c = ensureChart();
    const bars = data.bars || [];
    if (!bars.length) { c.clear(); toast('暂无 K 线数据'); return; }

    const dates = bars.map((b) => b.date);
    const ohlc = bars.map((b) => [n(b.open), n(b.close), n(b.low), n(b.high)]);
    const closes = bars.map((b) => n(b.close) || 0);
    const vols = bars.map((b) => ({
      value: n(b.volume) || 0,
      itemStyle: {
        color: (n(b.close) >= n(b.open)) ? 'rgba(240,65,62,.7)' : 'rgba(18,184,134,.7)',
      },
    }));

    const ma5 = movingAverage(closes, 5);
    const ma10 = movingAverage(closes, 10);
    const ma20 = movingAverage(closes, 20);
    const ma60 = movingAverage(closes, 60);

    const zoomStart = Math.max(0, 100 - (120 / bars.length) * 100);
    const isMinute = data.period && data.period.endsWith('m');

    c.setOption({
      animation: false,
      backgroundColor: 'transparent',
      textStyle: { fontFamily: 'Consolas, monospace' },
      legend: {
        data: ['MA5', 'MA10', 'MA20', 'MA60'], top: 0, right: 70,
        itemWidth: 14, itemHeight: 8, textStyle: { color: '#7d8fa6', fontSize: 11 },
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', label: { backgroundColor: '#2b3a4f' } },
        backgroundColor: 'rgba(24,34,49,.96)',
        borderColor: '#233044',
        textStyle: { color: '#d7e1ee', fontSize: 12 },
        formatter: (ps) => {
          if (!ps || !ps.length) return '';
          const i = ps[0].dataIndex;
          const b = bars[i];
          const cc = dirClass(b.change_pct);
          return `${b.date}<br/>` +
            `开 ${fmt(b.open)}　高 <span style="color:${COLOR.up}">${fmt(b.high)}</span><br/>` +
            `低 <span style="color:${COLOR.down}">${fmt(b.low)}</span>　收 <b style="color:${cc === 'up' ? COLOR.up : cc === 'down' ? COLOR.down : COLOR.flat}">${fmt(b.close)}</b><br/>` +
            `涨跌 <b>${pct(b.change_pct)}</b>　振幅 ${fmt(b.amplitude)}%<br/>` +
            `成交量 ${hand(b.volume)}<br/>成交额 ${money(b.amount)}`;
        },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: 62, right: 62, top: 26, height: '58%' },
        { left: 62, right: 62, top: '76%', height: '16%' },
      ],
      xAxis: [
        {
          type: 'category', data: dates, boundaryGap: true,
          axisLine: { lineStyle: { color: '#233044' } }, axisTick: { show: false },
          axisLabel: { color: '#5a6b80', fontSize: 10 },
          splitLine: { show: false },
        },
        {
          type: 'category', data: dates, gridIndex: 1, boundaryGap: true,
          axisLine: { lineStyle: { color: '#233044' } }, axisTick: { show: false },
          axisLabel: { show: false },
        },
      ],
      yAxis: [
        {
          gridIndex: 0, scale: true, splitNumber: 5,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#7d8fa6', fontSize: 10, formatter: (v) => fmt(v) },
          splitLine: { lineStyle: { color: '#1b2634' } },
        },
        {
          gridIndex: 1, splitNumber: 2,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#5a6b80', fontSize: 9, formatter: (v) => (v >= 10000 ? (v / 10000).toFixed(0) + '万' : v) },
          splitLine: { lineStyle: { color: '#1b2634' } },
        },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100 },
        {
          type: 'slider', xAxisIndex: [0, 1], bottom: 6, height: 16,
          start: zoomStart, end: 100,
          borderColor: '#233044', fillerColor: 'rgba(63,140,255,.15)',
          handleStyle: { color: '#3f8cff' },
          textStyle: { color: '#5a6b80', fontSize: 10 },
        },
      ],
      series: [
        {
          name: 'K线', type: 'candlestick', data: ohlc,
          itemStyle: {
            color: COLOR.up, color0: COLOR.down,
            borderColor: COLOR.up, borderColor0: COLOR.down,
          },
          z: 5,
        },
        { name: 'MA5', type: 'line', data: ma5, smooth: true, showSymbol: false, lineStyle: { width: 1, color: COLOR.ma5 }, itemStyle: { color: COLOR.ma5 } },
        { name: 'MA10', type: 'line', data: ma10, smooth: true, showSymbol: false, lineStyle: { width: 1, color: COLOR.ma10 }, itemStyle: { color: COLOR.ma10 } },
        { name: 'MA20', type: 'line', data: ma20, smooth: true, showSymbol: false, lineStyle: { width: 1, color: COLOR.ma20 }, itemStyle: { color: COLOR.ma20 } },
        { name: 'MA60', type: 'line', data: ma60, smooth: true, showSymbol: false, lineStyle: { width: 1, color: COLOR.ma60 }, itemStyle: { color: COLOR.ma60 } },
        { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: vols, barWidth: '60%' },
      ],
    }, true);
  }

  // ─────────────────────────── 周期切换 ───────────────────────────

  document.querySelectorAll('#chartTabs button[data-period]').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#chartTabs button[data-period]').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.period = btn.dataset.period;
      loadChart();
    });
  });
  $('adjustSelect').addEventListener('change', (e) => {
    state.adjust = e.target.value;
    if (state.period !== 'minute' && state.period !== 'minute5') loadChart();
  });
  $('autoRefresh').addEventListener('change', (e) => { state.autoRefresh = e.target.checked; });
  $('refreshBtn').addEventListener('click', () => { tick(true); });

  // ─────────────────────────── 主循环 ───────────────────────────

  let lastIndex = 0, lastMinute = 0, lastKline = 0, lastAux = 0, lastQuote = 0;

  async function tick(force) {
    const t = Date.now();
    $('clock').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });

    if (!state.autoRefresh && !force) return;

    if (force || t - lastIndex > INDEX_MS) { lastIndex = t; loadIndices(); loadStatus(); }

    // 非交易时段降低行情轮询频率，减轻上游压力
    const quoteEvery = state.trading ? QUOTE_MS : QUOTE_MS * 8;
    if (force || t - lastQuote > quoteEvery) {
      lastQuote = t;
      refreshWatchQuotes();
      refreshQuote();
    }

    if (force || t - lastMinute > MINUTE_MS) {
      lastMinute = t;
      if (state.period === 'minute' || state.period === 'minute5') loadChart();
    }
    if (force || t - lastKline > KLINE_MS) {
      lastKline = t;
      if (state.period !== 'minute' && state.period !== 'minute5') loadChart();
    }
    if (force || t - lastAux > MINUTE_MS) { lastAux = t; loadAux(); }
  }

  // ─────────────────────────── 启动 ───────────────────────────

  async function boot() {
    ensureChart();
    loadIndices();
    loadStatus();
    renderWatch();

    let start = null;
    try { start = JSON.parse(localStorage.getItem(LS_CURRENT) || 'null'); } catch (e) { /* ignore */ }
    if (!start || !start.code) start = { code: state.watch[0] || '600519' };

    await selectStock(start.code, start.name);
    await refreshWatchQuotes();

    tick(true);
    state.timer = setInterval(tick, QUOTE_MS);
  }

  boot();
})();
