'use strict';

/* JSK deal finder — frontend.
 *
 * No build step and no framework: the whole UI is a few render functions over
 * JSON from the local API. Analysis is stateless server-side, so the assumption
 * controls just re-request /api/candidates with different query params.
 */

const state = {
  boot: null,
  assumptions: {},
  defaults: {},
  candidates: [],
  view: 'candidates',
  jobTimer: null,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

/* --------------------------------------------------------------- format */

const money = (v) =>
  v === null || v === undefined ? '—' :
  (v < 0 ? '-$' : '$') + Math.abs(Math.round(v)).toLocaleString('en-US');
const compactMoney = (v) => {
  if (v === null || v === undefined) return '—';
  const abs = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(abs >= 1e7 ? 0 : 1)}M`;
  if (abs >= 1e3) return `${sign}$${Math.round(abs / 1e3)}k`;
  return `${sign}$${Math.round(abs)}`;
};
const num = (v, d = 0) =>
  v === null || v === undefined ? '—' : Number(v).toLocaleString('en-US',
    { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v, d = 1) => (v === null || v === undefined ? '—' : `${Number(v).toFixed(d)}%`);
const signedPct = (v) => (v === null || v === undefined ? '—' :
  `${v > 0 ? '+' : ''}${Number(v).toFixed(0)}%`);
const cls = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : '');
const shortDate = (s) => (s ? String(s).slice(0, 10) : '—');

function toast(message, bad = false) {
  const node = $('#toast');
  node.textContent = message;
  node.className = bad ? 'toast bad' : 'toast';
  node.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { node.hidden = true; }, bad ? 7000 : 3200);
}

async function api(path, options) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

/* ------------------------------------------------- assumption controls */

/* Grouped so the rail reads as a story: what you pay, what the work costs,
 * how you exit, and what you refuse to believe. */
const CONTROL_GROUPS = [
  {
    title: 'Acquisition',
    controls: [
      { key: 'negotiation_discount', label: 'Negotiation discount', min: 0, max: 0.3, step: 0.01, kind: 'pct',
        desc: 'How far under asking you expect to buy' },
      { key: 'purchase_fees_pct', label: 'Purchase fees', min: 0, max: 0.15, step: 0.005, kind: 'pct',
        desc: 'Registration, notary, legal — verify current rates' },
    ],
  },
  {
    title: 'Renovation ($/m²)',
    controls: [
      { key: 'reno_cost_light', label: 'Light', min: 0, max: 600, step: 10, kind: 'money' },
      { key: 'reno_cost_medium', label: 'Medium', min: 0, max: 900, step: 10, kind: 'money' },
      { key: 'reno_cost_full', label: 'Full gut', min: 0, max: 1500, step: 25, kind: 'money' },
      { key: 'reno_contingency_pct', label: 'Contingency', min: 0, max: 0.5, step: 0.05, kind: 'pct' },
    ],
  },
  {
    title: 'Holding & exit',
    controls: [
      { key: 'holding_months', label: 'Holding period', min: 1, max: 36, step: 1, kind: 'months' },
      { key: 'holding_cost_per_month', label: 'Monthly holding cost', min: 0, max: 2000, step: 50, kind: 'money' },
      { key: 'sale_commission_pct', label: 'Sale commission', min: 0, max: 0.08, step: 0.005, kind: 'pct' },
      { key: 'exit_percentile', label: 'Exit percentile', min: 0.3, max: 0.9, step: 0.05, kind: 'pct',
        desc: 'Where you sell within the finished-comp range' },
    ],
  },
  {
    title: 'Credibility guardrails',
    controls: [
      { key: 'max_resale_uplift', label: 'Max resale uplift', min: 1.2, max: 6, step: 0.1, kind: 'x',
        desc: 'Above this multiple of asking/m², bad comps are likelier than a bargain' },
      { key: 'min_comps', label: 'Minimum comps', min: 1, max: 25, step: 1, kind: 'plain' },
      { key: 'comp_area_tolerance', label: 'Comp size tolerance', min: 0.1, max: 1, step: 0.05, kind: 'pct' },
    ],
  },
  {
    title: 'Screens',
    controls: [
      { key: 'min_profit_usd', label: 'Minimum profit', min: 0, max: 300000, step: 5000, kind: 'money' },
      { key: 'min_roi', label: 'Minimum ROI', min: 0, max: 1, step: 0.05, kind: 'pct' },
      { key: 'min_area_m2', label: 'Minimum size', min: 0, max: 400, step: 10, kind: 'area' },
    ],
  },
];

const showValue = (kind, value) => {
  switch (kind) {
    case 'pct': return `${(value * 100).toFixed(value < 0.1 ? 1 : 0)}%`;
    case 'money': return money(value);
    case 'months': return `${value} mo`;
    case 'area': return `${value} m²`;
    case 'x': return `${Number(value).toFixed(1)}×`;
    default: return num(value);
  }
};

function renderControls() {
  const host = $('#controls');
  host.textContent = '';

  for (const group of CONTROL_GROUPS) {
    const fieldset = el('fieldset', { class: 'group' }, el('legend', {}, group.title));
    for (const spec of group.controls) {
      const value = state.assumptions[spec.key];
      const out = el('output', {}, showValue(spec.kind, value));
      const slider = el('input', {
        type: 'range', min: spec.min, max: spec.max, step: spec.step, value,
        'aria-label': spec.label,
        oninput: (event) => {
          const next = Number(event.target.value);
          state.assumptions[spec.key] = next;
          out.textContent = showValue(spec.kind, next);
          scheduleCandidates();
        },
      });
      fieldset.append(el('div', { class: 'control' },
        el('div', { class: 'row' }, el('label', {}, spec.label), out),
        slider,
        spec.desc ? el('div', { class: 'desc' }, spec.desc) : null,
      ));
    }
    host.append(fieldset);
  }

  // Max benchmark scope is a choice, not a magnitude.
  const scope = el('select', {
    onchange: (event) => {
      state.assumptions.max_benchmark_scope = event.target.value;
      scheduleCandidates();
    },
  }, ['town', 'district', 'governorate', 'global'].map((v) =>
    el('option', { value: v, selected: state.assumptions.max_benchmark_scope === v ? '' : null }, v)));
  const budget = el('input', {
    type: 'number', step: 25000, min: 0, placeholder: 'no ceiling',
    value: state.assumptions.max_price_usd ?? '',
    oninput: (event) => {
      const raw = event.target.value;
      state.assumptions.max_price_usd = raw === '' ? null : Number(raw);
      scheduleCandidates();
    },
  });
  host.append(el('fieldset', { class: 'group' },
    el('legend', {}, 'Scope & budget'),
    el('div', { class: 'control' },
      el('div', { class: 'row' }, el('label', {}, 'Widest comp scope allowed')), scope,
      el('div', { class: 'desc' }, 'Benchmarks borrowed from wider than this are excluded')),
    el('div', { class: 'control' },
      el('div', { class: 'row' }, el('label', {}, 'Budget ceiling')), budget),
  ));
}

/* --------------------------------------------------------- candidates */

let candidateTimer = null;
function scheduleCandidates() {
  clearTimeout(candidateTimer);
  candidateTimer = setTimeout(loadCandidates, 160);
}

function assumptionQuery() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(state.assumptions)) {
    params.set(key, value === null ? 'null' : value);
  }
  params.set('condition', $('#f-condition').value);
  params.set('town', $('#f-town').value);
  params.set('sort', $('#f-sort').value);
  params.set('screens', $('#f-screens').value);
  params.set('limit', '300');
  return params;
}

async function loadCandidates() {
  const body = $('#candidates-body');
  try {
    const data = await api(`/api/candidates?${assumptionQuery()}`);
    state.candidates = data.candidates;
    renderCandidateKpis(data);
    renderThesisNote(data);
    body.textContent = '';
    body.append(candidateTable(data.candidates));
  } catch (error) {
    body.textContent = '';
    body.append(el('p', { class: 'empty' }, `Could not analyse: ${error.message}`));
  }
}

function renderCandidateKpis(data) {
  const b = data.breakdown || {};
  const reno = (b.renovation_target || 0) + (b.neutral || 0);
  const best = data.candidates[0];
  $('#candidate-kpis').replaceChildren(
    tile('Candidates', num(data.total), `of ${num(data.analysed)} analysed`),
    tile('Renovation thesis', num(reno), 'needs-work or plain stock'),
    tile('Best expected profit', best ? compactMoney(best.profit_usd) : '—',
      best ? `${best.ref} · ${best.town || '—'}` : 'nothing passed'),
    tile('Best ROI', best ? pct(Math.max(...data.candidates.map((c) => c.roi_pct)), 0) : '—',
      'on all-in cost'),
  );
}

const tile = (label, value, note) =>
  el('div', { class: 'tile' },
    el('div', { class: 'label' }, label),
    el('div', { class: 'value' }, value),
    note ? el('div', { class: 'note' }, note) : null);

function renderThesisNote(data) {
  const b = data.breakdown || {};
  const order = ['renovation_target', 'neutral', 'unknown', 'finished'];
  const parts = order.filter((k) => b[k]).map((k) => `${b[k]} ${k.replace(/_/g, ' ')}`);
  $('#thesis-note').innerHTML =
    `<strong>Two theses are mixed here.</strong> ${parts.length ? 'Of these, ' + parts.join(', ') + '.' : ''}
     A <em>renovation target</em> is cheap because it needs work — renovation is what unlocks the value.
     A <em>finished</em> listing that still looks cheap against its comps is a different bet: buying under
     market, where the gap is usually explained by something the data cannot see (floor, view, exact street,
     building age). Switch the Thesis filter to isolate one or the other.`;
}

function candidateTable(rows) {
  if (!rows.length) {
    return el('p', { class: 'empty' },
      'Nothing passed. Loosen the screens on the left, or set Screens to “off” to see what was rejected and why.');
  }
  const headers = ['Property', 'Town', 'm²#', 'Asking#', 'Ask $/m²#', 'Resale $/m²#',
    'vs mkt#', 'Reno#', 'All-in#', 'Net exit#', 'Profit#', 'ROI#', 'Confidence', 'Flags'];
  const body = rows.map((c) => el('tr', { onclick: () => openDrawer(c.ref) },
    el('td', { class: 'wrap' },
      el('div', {}, c.title || c.ref),
      el('div', { style: 'margin-top:.2rem; display:flex; gap:.25rem; flex-wrap:wrap' },
        el('span', { class: 'chip' }, c.ref),
        conditionChip(c.condition_label),
        el('span', { class: 'chip' }, `reno: ${c.reno_depth}`))),
    el('td', {}, c.town || '—'),
    el('td', { class: 'num' }, num(c.area_m2)),
    el('td', { class: 'num' }, money(c.asking_price)),
    el('td', { class: 'num' }, num(c.asking_ppm2)),
    el('td', { class: 'num' }, num(c.resale_ppm2)),
    el('td', { class: 'num ' + cls(c.discount_to_market_pct) }, signedPct(c.discount_to_market_pct)),
    el('td', { class: 'num' }, compactMoney(c.reno_cost)),
    el('td', { class: 'num' }, compactMoney(c.all_in_cost)),
    el('td', { class: 'num' }, compactMoney(c.net_resale)),
    el('td', { class: 'num ' + cls(c.profit_usd) }, compactMoney(c.profit_usd)),
    el('td', { class: 'num ' + cls(c.roi_pct) }, pct(c.roi_pct)),
    el('td', {}, el('span', { class: 'chip ' + (c.confidence === 'high' ? 'high' : c.confidence === 'low' ? 'low' : '') },
      c.confidence), ' ', el('span', { class: 'muted' }, `n=${c.n_comps}`)),
    el('td', {}, c.flags ? el('span', { class: 'flagtext' }, c.flags) : el('span', { class: 'muted' }, '—')),
  ));
  return dataTable(headers, body);
}

function exportCandidates() {
  if (!state.candidates.length) return toast('Nothing to export.', true);
  const cols = Object.keys(state.candidates[0]);
  const escape = (v) => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [cols.join(',')]
    .concat(state.candidates.map((row) => cols.map((c) => escape(row[c])).join(',')))
    .join('\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  const link = el('a', { href: url, download: 'flip_candidates.csv' });
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/* ----------------------------------------------------------- listings */

let listingTimer = null;
function scheduleListings() {
  clearTimeout(listingTimer);
  listingTimer = setTimeout(() => loadListings(1), 250);
}

async function loadListings(page = 1) {
  const params = new URLSearchParams({
    page, per_page: '60',
    q: $('#l-q').value,
    town: $('#l-town').value,
    active: $('#l-active').value,
  });
  for (const [id, key] of [['#l-min-price', 'min_price'], ['#l-max-price', 'max_price'],
    ['#l-min-area', 'min_area']]) {
    if ($(id).value) params.set(key, $(id).value);
  }
  const host = $('#listings-body');
  try {
    const data = await api(`/api/listings?${params}`);
    host.textContent = '';
    if (!data.listings.length) {
      host.append(el('p', { class: 'empty' }, 'No listings match those filters.'));
      return;
    }
    const rows = data.listings.map((l) => el('tr', { onclick: () => openDrawer(l.ref) },
      el('td', { class: 'wrap' }, l.title || l.ref,
        el('div', {}, el('span', { class: 'chip' }, l.ref),
          !l.is_active ? el('span', { class: 'chip low' }, 'delisted') : null)),
      el('td', {}, l.town || '—'),
      el('td', {}, l.property_type || '—'),
      el('td', { class: 'num' }, num(l.area_m2)),
      el('td', { class: 'num' }, money(l.price_usd)),
      el('td', { class: 'num' }, num(l.price_per_m2)),
      el('td', { class: 'num' }, num(l.bedrooms)),
      el('td', {}, conditionChip(l.condition_label)),
      el('td', { class: 'num' }, l.price_changes || 0),
      el('td', { class: 'muted' }, shortDate(l.first_seen)),
    ));
    host.append(
      dataTable(['Property', 'Town', 'Type', 'm²#', 'Price#', '$/m²#', 'Beds#',
        'Condition', 'Cuts#', 'First seen'], rows),
      pager(data));
  } catch (error) {
    host.textContent = '';
    host.append(el('p', { class: 'empty' }, `Could not load: ${error.message}`));
  }
}

const pager = (data) => el('div', {
  style: 'display:flex; align-items:center; gap:.6rem; margin-top:.7rem; font-size:.82rem',
},
  el('button', {
    class: 'action ghost', disabled: data.page <= 1 ? '' : null,
    onclick: () => loadListings(data.page - 1),
  }, '← Prev'),
  el('span', { class: 'muted' }, `Page ${data.page} of ${data.pages} · ${num(data.total)} listings`),
  el('button', {
    class: 'action ghost', disabled: data.page >= data.pages ? '' : null,
    onclick: () => loadListings(data.page + 1),
  }, 'Next →'));

/* ------------------------------------------------------------- market */

async function loadMarket() {
  const host = $('#market-body');
  try {
    const data = await api(`/api/market?${assumptionQuery()}`);
    const min = Number($('#m-min').value);
    const rows = data.market.filter((m) => m.listings >= min);
    $('#market-chart').replaceChildren(spreadChart(rows.slice(0, 18)));
    host.textContent = '';
    if (!rows.length) {
      host.append(el('p', { class: 'empty' }, 'No towns meet that listing count yet.'));
      return;
    }
    const marketRow = (m) => el('tr', {
      onclick: () => { $('#f-town').value = m.town; switchView('candidates'); loadCandidates(); },
    },
      el('td', {}, m.town),
      el('td', { class: 'muted' }, m.district || '—'),
      el('td', { class: 'num' }, m.listings),
      el('td', { class: 'num' }, num(m.p25_ppm2)),
      el('td', { class: 'num' }, num(m.median_ppm2)),
      el('td', { class: 'num' }, num(m.p75_ppm2)),
      el('td', { class: 'num' }, `${m.spread_pct}%`),
      el('td', { class: 'num' }, m.renovation_targets),
      el('td', { class: 'num' }, m.finished));

    host.append(dataTable(
      ['Town', 'District', 'Listings#', 'P25 $/m²#', 'Median $/m²#', 'P75 $/m²#',
        'Spread#', 'Reno targets#', 'Finished#'],
      rows.map(marketRow)));
  } catch (error) {
    host.textContent = '';
    host.append(el('p', { class: 'empty' }, `Could not load: ${error.message}`));
  }
}

const SVG = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs = {}, ...kids) => {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

/* Range plot: P25→P75 per town with a median tick. One hue — the bars encode a
 * span, not separate identities, so no legend is needed; the axis names the
 * measure and the table below carries every number. */
function spreadChart(rows) {
  if (!rows.length) return el('div');
  const rowH = 22, padL = 132, padR = 58, padT = 26, padB = 26;
  const width = 900;
  const height = padT + rows.length * rowH + padB;
  const max = Math.max(...rows.map((r) => r.p75_ppm2)) * 1.05;
  const x = (v) => padL + (v / max) * (width - padL - padR);

  const ticks = [];
  const step = max > 6000 ? 2000 : max > 3000 ? 1000 : 500;
  for (let v = 0; v <= max; v += step) ticks.push(v);

  const kids = [];
  for (const v of ticks) {
    kids.push(svgEl('line', { class: 'gridline', x1: x(v), x2: x(v), y1: padT - 6, y2: height - padB }));
    kids.push(svgEl('text', { class: 'tick', x: x(v), y: padT - 12, 'text-anchor': 'middle' },
      v === 0 ? '0' : `$${v / 1000}k`));
  }

  rows.forEach((r, i) => {
    const y = padT + i * rowH + rowH / 2;
    kids.push(svgEl('text', {
      class: 'tick', x: padL - 8, y: y + 3.5, 'text-anchor': 'end',
    }, `${r.town.slice(0, 18)} (${r.listings})`));
    // The span, as a thin rounded bar.
    kids.push(svgEl('rect', {
      x: x(r.p25_ppm2), y: y - 5, width: Math.max(2, x(r.p75_ppm2) - x(r.p25_ppm2)),
      height: 10, rx: 4, fill: 'var(--seq-450)', opacity: .5,
      onmousemove: (e) => showTip(e, r.town, [
        ['Listings', r.listings], ['P25 $/m²', num(r.p25_ppm2)],
        ['Median $/m²', num(r.median_ppm2)], ['P75 $/m²', num(r.p75_ppm2)],
        ['Spread', `${r.spread_pct}%`], ['Reno targets', r.renovation_targets],
        ['Finished comps', r.finished],
      ]),
      onmouseleave: hideTip,
    }));
    // Median tick — the second shade of the same hue.
    kids.push(svgEl('line', {
      x1: x(r.median_ppm2), x2: x(r.median_ppm2), y1: y - 8, y2: y + 8,
      stroke: 'var(--seq-600)', 'stroke-width': 2,
    }));
    kids.push(svgEl('text', {
      class: 'dlabel', x: x(r.p75_ppm2) + 6, y: y + 3.5,
    }, `${r.spread_pct}%`));
  });

  kids.push(svgEl('line', { class: 'axisline', x1: padL, x2: padL, y1: padT - 6, y2: height - padB }));

  return el('figure', { class: 'chart' },
    el('figcaption', {},
      'Asking $/m² spread by town — bar spans the 25th to 75th percentile, the dark tick is the median, the label is the spread as a share of the median.'),
    svgEl('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img' }, kids),
    el('div', { class: 'legend' },
      el('span', { class: 'key' },
        el('span', { class: 'swatch', style: 'background:var(--seq-450); opacity:.5' }), 'P25–P75 range'),
      el('span', { class: 'key' },
        el('span', { class: 'swatch', style: 'background:var(--seq-600); width:3px; height:12px; border-radius:1px' }), 'Median')));
}

/* ------------------------------------------------------------ changes */

async function loadChanges() {
  const host = $('#changes-body');
  try {
    const data = await api(`/api/changes?days=${$('#c-days').value}`);
    host.textContent = '';

    host.append(el('h2', { class: 'section' }, `Price reductions (${data.price_cuts.length})`));
    if (!data.price_cuts.length) {
      host.append(el('p', { class: 'empty' },
        'No reductions recorded yet. These only appear once the same listing has been crawled at two different prices — run the crawl on a schedule and this fills in.'));
    } else {
      const cutRow = (r) => el('tr', { onclick: () => openDrawer(r.ref) },
        el('td', { class: 'wrap' }, r.title || r.ref, ' ', el('span', { class: 'chip' }, r.ref)),
        el('td', {}, r.town || '—'),
        el('td', { class: 'num' }, money(r.first_price_usd)),
        el('td', { class: 'num' }, money(r.price_usd)),
        el('td', { class: 'num neg' }, compactMoney(-r.drop_usd)),
        el('td', { class: 'num neg' }, pct(-r.drop_pct)),
        el('td', { class: 'num' }, r.price_changes));

      host.append(dataTable(
        ['Property', 'Town', 'Was#', 'Now#', 'Cut#', '%#', 'Changes#'],
        data.price_cuts.map(cutRow)));
    }

    host.append(el('h2', { class: 'section' }, `Delisted (${data.delisted.length})`));
    if (!data.delisted.length) {
      host.append(el('p', { class: 'empty' },
        'Nothing has dropped out yet. A listing is only retired after two consecutive complete crawls miss it.'));
    } else {
      const goneRow = (r) => el('tr', { onclick: () => openDrawer(r.ref) },
        el('td', { class: 'wrap' }, r.title || r.ref, ' ', el('span', { class: 'chip' }, r.ref)),
        el('td', {}, r.town || '—'),
        el('td', { class: 'num' }, money(r.price_usd)),
        el('td', { class: 'num' }, num(r.area_m2)),
        el('td', {}, shortDate(r.delisted_at)),
        el('td', { class: 'muted' }, shortDate(r.first_seen)));

      host.append(dataTable(
        ['Property', 'Town', 'Last price#', 'm²#', 'Gone since', 'First seen'],
        data.delisted.map(goneRow)));
    }
  } catch (error) {
    host.textContent = '';
    host.append(el('p', { class: 'empty' }, `Could not load: ${error.message}`));
  }
}

/* --------------------------------------------------------------- data */

function renderDataView() {
  const s = state.boot.stats;
  $('#data-kpis').replaceChildren(
    tile('Active listings', num(s.active), `${num(s.total)} ever seen`),
    tile('Delisted', num(s.delisted), 'often means sold'),
    tile('Full descriptions', num(s.with_detail), `${num(s.active - s.with_detail)} still from cards`),
    tile('Price observations', num(s.price_changes), `${num(s.runs)} crawls`),
  );

  const runs = state.boot.runs || [];
  if (!runs.length) {
    $('#runs-body').replaceChildren(el('p', { class: 'empty' }, 'No crawls recorded yet.'));
    return;
  }

  const statusChip = (status) => el('span', {
    class: 'chip ' + (status === 'ok' ? 'high' : status === 'running' ? '' : 'low'),
  }, status);

  const runRow = (r) => el('tr', { style: 'cursor:default' },
    el('td', {}, String(r.started_at).replace('T', ' ').slice(0, 16)),
    el('td', {}, r.category || '—'),
    el('td', { class: 'num' }, r.pages_fetched || 0),
    el('td', { class: 'num' }, num(r.seen)),
    el('td', { class: 'num' }, num(r.new_listings)),
    el('td', { class: 'num' }, num(r.price_cuts)),
    el('td', { class: 'num' }, num(r.delisted)),
    el('td', {}, statusChip(r.status)));

  $('#runs-body').replaceChildren(dataTable(
    ['Started', 'Category', 'Pages#', 'Seen#', 'New#', 'Cuts#', 'Delisted#', 'Status'],
    runs.map(runRow)));
}

/* Table shell. A header label ending in '#' marks a numeric column, which keeps
 * the call sites flat -- the nested el() soup this replaced hid a syntax error
 * that only surfaced at runtime. */
function dataTable(headers, rows, extraClass = '') {
  const head = el('tr', {}, headers.map((label) => {
    const numeric = label.endsWith('#');
    return el('th', { class: numeric ? 'num' : null }, numeric ? label.slice(0, -1) : label);
  }));
  return el('div', { class: `scroll ${extraClass}` },
    el('table', {}, el('thead', {}, head), el('tbody', {}, rows)));
}

async function startJob(kind) {
  const payload = { kind };
  if (kind === 'scrape') {
    payload.category = $('#j-category').value;
    if ($('#j-max-pages').value) payload.max_pages = Number($('#j-max-pages').value);
  } else {
    payload.limit = 300;
  }
  try {
    const data = await api('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    toast(`Started ${kind} job #${data.job.id}.`);
    pollJobs();
  } catch (error) {
    toast(error.message, true);
  }
}

async function pollJobs() {
  try {
    const { jobs } = await api('/api/jobs');
    const running = jobs.find((j) => j.status === 'running');
    const bar = $('#jobbar');
    $('#stop-job').hidden = !running;

    if (!running) {
      const last = jobs[0];
      if (state.jobTimer) {
        clearInterval(state.jobTimer);
        state.jobTimer = null;
        // A finished crawl changes the data underneath every view.
        await bootstrap({ keepAssumptions: true });
        if (state.view === 'candidates') loadCandidates();
        if (state.view === 'data') renderDataView();
      }
      if (last && last.status !== 'running') {
        bar.hidden = false;
        $('#job-title').textContent = `${last.kind} #${last.id} — ${last.status}`;
        $('#job-bar').style.width = '100%';
        $('#job-detail').textContent = last.error
          ? last.error
          : describeProgress(last.kind, last.progress);
      } else {
        bar.hidden = true;
      }
      return;
    }

    bar.hidden = false;
    $('#job-title').textContent = `${running.kind} #${running.id}`;
    const p = running.progress || {};
    const done = running.kind === 'scrape' ? (p.pages_fetched || 0) : (p.done || 0);
    const total = running.kind === 'scrape' ? (p.last_page || 0) : (p.total || 0);
    $('#job-bar').style.width = total ? `${Math.min(100, (done / total) * 100)}%` : '8%';
    $('#job-detail').textContent = describeProgress(running.kind, p);

    if (!state.jobTimer) state.jobTimer = setInterval(pollJobs, 2000);
  } catch {
    /* transient — the next tick retries */
  }
}

function describeProgress(kind, p = {}) {
  if (kind === 'scrape') {
    return `page ${num(p.pages_fetched)} of ${num(p.last_page)} · ${num(p.seen)} listings · `
      + `${num(p.new_listings)} new · ${num(p.price_cuts)} cuts`
      + (p.delisted ? ` · ${num(p.delisted)} delisted` : '');
  }
  return `${num(p.done ?? p.fetched)} of ${num(p.total)} fetched`;
}

async function stopJob() {
  const { jobs } = await api('/api/jobs');
  const running = jobs.find((j) => j.status === 'running');
  if (!running) return;
  await api(`/api/jobs/${running.id}/stop`, { method: 'POST' });
  toast('Stop requested — the crawl finishes its current page and exits.');
}

/* ------------------------------------------------------------- drawer */

async function openDrawer(ref) {
  const drawer = $('#drawer');
  const host = $('#drawer-content');
  drawer.hidden = false;
  host.replaceChildren(el('p', { class: 'loading' }, 'Loading…'));
  try {
    const params = assumptionQuery();
    const d = await api(`/api/listings/${encodeURIComponent(ref)}?${params}`);
    host.replaceChildren(...drawerContent(d));
  } catch (error) {
    host.replaceChildren(el('p', { class: 'empty' }, `Could not load ${ref}: ${error.message}`));
  }
}

function closeDrawer() { $('#drawer').hidden = true; }

function drawerContent(d) {
  const deal = d.deal;
  const parts = [
    el('header', {},
      el('div', {},
        el('h3', {}, d.title || d.ref),
        el('div', { class: 'muted', style: 'font-size:.8rem; margin-top:.2rem' },
          [d.location_raw || d.town, d.ref, d.property_type].filter(Boolean).join(' · '))),
      el('button', { class: 'close', onclick: closeDrawer, 'aria-label': 'Close' }, '✕')),
    el('p', { style: 'margin:.5rem 0 1rem' },
      el('a', { href: `https://www.jskre.com${d.url}`, target: '_blank', rel: 'noopener' },
        'Open on jskre.com ↗')),
    el('dl', { class: 'kv' },
      el('dt', {}, 'Asking'), el('dd', {}, money(d.price_usd)),
      el('dt', {}, 'Area'), el('dd', {}, `${num(d.area_m2)} m²`),
      el('dt', {}, 'Asking $/m²'), el('dd', {}, num(d.price_per_m2)),
      el('dt', {}, 'Beds / baths'), el('dd', {}, `${num(d.bedrooms)} / ${num(d.bathrooms)}`),
      el('dt', {}, 'Condition'), el('dd', {},
        d.condition_label.replace(/_/g, ' '),
        d.condition_signals.length ? ` — ${d.condition_signals.join('; ')}` : ''),
      el('dt', {}, 'First seen'), el('dd', {}, `${shortDate(d.first_seen)} (${d.price_changes || 0} price changes)`),
    ),
  ];

  if (d.image_urls && d.image_urls.length) {
    parts.push(el('h2', { class: 'section' }, 'Photos'),
      el('div', { class: 'gallery' },
        d.image_urls.slice(0, 10).map((src) =>
          el('img', { src, alt: 'listing photo', loading: 'lazy' }))));
  }

  if (deal) {
    parts.push(el('h2', { class: 'section' }, 'Where the money goes'),
      costChart(deal),
      el('dl', { class: 'kv', style: 'margin-top:.8rem' },
        el('dt', {}, 'Resale benchmark'),
        el('dd', {}, `${num(deal.resale_ppm2)} $/m² — ${deal.benchmark_scope} level “${deal.benchmark_key}”, n=${deal.n_comps}`),
        el('dt', {}, 'Modelled profit'),
        el('dd', { class: cls(deal.profit_usd) }, `${money(deal.profit_usd)} · ROI ${pct(deal.roi_pct)} · margin ${pct(deal.margin_pct)}`),
        el('dt', {}, 'Confidence'), el('dd', {}, deal.confidence),
        el('dt', {}, 'Uplift ratio'), el('dd', {}, `${deal.resale_uplift_ratio}× asking $/m²`)),
      deal.flags ? el('p', { class: 'note-box', style: 'margin-top:.8rem' },
        el('strong', {}, 'Flags: '), deal.flags) : null);
  } else {
    parts.push(el('p', { class: 'note-box', style: 'margin-top:.9rem' },
      'No deal maths for this listing — it is missing a price or a floor area.'));
  }

  if (d.price_history && d.price_history.length > 1) {
    parts.push(el('h2', { class: 'section' }, 'Asking price history'), priceChart(d.price_history));
  }

  if (d.description) {
    parts.push(el('h2', { class: 'section' }, 'Description'),
      el('div', { class: 'desc-text' }, d.description),
      d.description_truncated
        ? el('p', { class: 'footnote' }, 'Truncated — run “Fill missing descriptions” on the Data tab for the full text.')
        : null);
  }

  if (d.peers && d.peers.length) {
    parts.push(el('h2', { class: 'section' }, `Other listings in ${d.town || 'this town'} (${d.peers.length})`),
      el('p', { class: 'footnote' }, 'Sorted cheapest first by $/m², so you can eyeball whether the benchmark is fair.'),
      peerTable(d.peers));
  }

  return parts.filter(Boolean);
}

const conditionChip = (label) => el('span', {
  class: 'chip ' + (label === 'renovation_target' ? 'reno'
    : label === 'finished' ? 'finished' : ''),
}, label.replace(/_/g, ' '));

function peerTable(peers) {
  const row = (p) => el('tr', { onclick: () => openDrawer(p.ref) },
    el('td', {}, p.ref),
    el('td', { class: 'num' }, num(p.area_m2)),
    el('td', { class: 'num' }, money(p.price_usd)),
    el('td', { class: 'num' }, num(p.price_per_m2)),
    el('td', {}, conditionChip(p.condition_label)));
  const table = dataTable(['Ref', 'm²#', 'Price#', '$/m²#', 'Condition'], peers.map(row));
  table.style.cssText = 'margin-top:.4rem; max-height:320px; overflow-y:auto';
  return table;
}

/* Cost stack vs exit. Part-to-whole against a comparison total, so: horizontal
 * stacked bar. Four categorical slots, each direct-labelled — required relief,
 * since two of the light-mode slots sit below 3:1 on the surface. */
function costChart(deal) {
  const segments = [
    { label: 'Purchase', value: deal.purchase_price, color: 'var(--series-1)' },
    { label: 'Fees', value: deal.purchase_fees, color: 'var(--series-2)' },
    { label: 'Renovation', value: deal.reno_cost, color: 'var(--series-3)' },
    { label: 'Holding', value: deal.holding_cost, color: 'var(--series-4)' },
  ];
  const width = 780, padL = 76, padR = 96, barH = 34, gap = 16, padT = 8;
  const height = padT + barH * 2 + gap + 34;
  const max = Math.max(deal.all_in_cost, deal.net_resale) * 1.02;
  const scale = (v) => (v / max) * (width - padL - padR);

  const kids = [];
  let cursor = padL;
  for (const seg of segments) {
    const w = scale(seg.value);
    if (w <= 0) continue;
    // 2px surface gap between adjacent fills.
    kids.push(svgEl('rect', {
      x: cursor, y: padT, width: Math.max(1, w - 2), height: barH, rx: 4, fill: seg.color,
      onmousemove: (e) => showTip(e, seg.label, [
        ['Amount', money(seg.value)],
        ['Share of all-in', pct((seg.value / deal.all_in_cost) * 100)],
      ]),
      onmouseleave: hideTip,
    }));
    if (w > 58) {
      kids.push(svgEl('text', {
        class: 'dlabel on-fill', x: cursor + 6, y: padT + barH / 2 + 4,
      }, `${seg.label} ${compactMoney(seg.value)}`));
    }
    cursor += w;
  }
  kids.push(svgEl('text', { class: 'tick', x: padL - 8, y: padT + barH / 2 + 4, 'text-anchor': 'end' }, 'All-in'));
  kids.push(svgEl('text', { class: 'dlabel', x: cursor + 8, y: padT + barH / 2 + 4 }, money(deal.all_in_cost)));

  const y2 = padT + barH + gap;
  const exitW = scale(deal.net_resale);
  kids.push(svgEl('rect', {
    x: padL, y: y2, width: Math.max(1, exitW), height: barH, rx: 4,
    fill: 'var(--seq-200)', stroke: 'var(--seq-450)', 'stroke-width': 1.5,
    onmousemove: (e) => showTip(e, 'Net resale', [
      ['Gross', money(deal.gross_resale)],
      ['Selling costs', money(-deal.selling_costs)],
      ['Net', money(deal.net_resale)],
    ]),
    onmouseleave: hideTip,
  }));
  kids.push(svgEl('text', { class: 'tick', x: padL - 8, y: y2 + barH / 2 + 4, 'text-anchor': 'end' }, 'Net exit'));
  kids.push(svgEl('text', { class: 'dlabel', x: padL + exitW + 8, y: y2 + barH / 2 + 4 }, money(deal.net_resale)));

  // The gap between the two bars IS the profit; say so explicitly.
  const profitColor = deal.profit_usd >= 0 ? 'var(--delta-up)' : 'var(--critical)';
  kids.push(svgEl('text', {
    x: padL, y: height - 8, fill: profitColor, 'font-size': 12, 'font-weight': 600,
  }, `${deal.profit_usd >= 0 ? 'Profit' : 'Loss'} ${money(deal.profit_usd)} · ROI ${pct(deal.roi_pct)}`));

  return el('figure', { class: 'chart' },
    el('figcaption', {}, 'All-in cost broken into its parts, against the modelled net resale. The difference between the two bars is the margin.'),
    svgEl('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img' }, kids),
    el('div', { class: 'legend' }, segments.map((s) =>
      el('span', { class: 'key' },
        el('span', { class: 'swatch', style: `background:${s.color}` }), s.label)).concat(
      el('span', { class: 'key' },
        el('span', { class: 'swatch', style: 'background:var(--seq-200); border:1.5px solid var(--seq-450)' }),
        'Net resale'))));
}

/* Asking price over time — one series, so a line with no legend; the caption
 * names the measure. Crosshair + tooltip on hover. */
function priceChart(history) {
  const width = 760, height = 190, padL = 62, padR = 18, padT = 14, padB = 30;
  const points = history.map((h) => ({ t: new Date(h.observed_at).getTime(), v: h.price_usd }))
    .filter((p) => Number.isFinite(p.t) && p.v !== null);
  if (points.length < 2) return el('div');

  const t0 = points[0].t, t1 = points[points.length - 1].t;
  const values = points.map((p) => p.v);
  const lo = Math.min(...values) * 0.96, hi = Math.max(...values) * 1.04;
  const x = (t) => padL + (t1 === t0 ? 0.5 : (t - t0) / (t1 - t0)) * (width - padL - padR);
  const y = (v) => padT + (1 - (v - lo) / (hi - lo || 1)) * (height - padT - padB);

  const kids = [];
  for (let i = 0; i <= 3; i++) {
    const v = lo + ((hi - lo) * i) / 3;
    kids.push(svgEl('line', { class: 'gridline', x1: padL, x2: width - padR, y1: y(v), y2: y(v) }));
    kids.push(svgEl('text', { class: 'tick', x: padL - 8, y: y(v) + 3.5, 'text-anchor': 'end' },
      compactMoney(v)));
  }
  kids.push(svgEl('line', { class: 'axisline', x1: padL, x2: width - padR, y1: height - padB, y2: height - padB }));
  // Asking prices hold flat between observations, so step the line rather than
  // interpolating a drift that never happened.
  let path = `M ${x(points[0].t)} ${y(points[0].v)}`;
  for (let i = 1; i < points.length; i++) {
    path += ` L ${x(points[i].t)} ${y(points[i - 1].v)} L ${x(points[i].t)} ${y(points[i].v)}`;
  }
  kids.push(svgEl('path', { d: path, fill: 'none', stroke: 'var(--series-1)', 'stroke-width': 2 }));

  points.forEach((p) => {
    kids.push(svgEl('circle', {
      cx: x(p.t), cy: y(p.v), r: 4.5, fill: 'var(--series-1)',
      stroke: 'var(--surface-1)', 'stroke-width': 2,
    }));
    kids.push(svgEl('circle', {
      cx: x(p.t), cy: y(p.v), r: 11, fill: 'transparent',
      onmousemove: (e) => showTip(e, new Date(p.t).toISOString().slice(0, 10),
        [['Asking', money(p.v)]]),
      onmouseleave: hideTip,
    }));
  });
  kids.push(svgEl('text', { class: 'tick', x: padL, y: height - 10 },
    new Date(t0).toISOString().slice(0, 10)));
  kids.push(svgEl('text', { class: 'tick', x: width - padR, y: height - 10, 'text-anchor': 'end' },
    new Date(t1).toISOString().slice(0, 10)));

  return el('figure', { class: 'chart' },
    el('figcaption', {}, 'Asking price as observed by this tool, stepped between crawls.'),
    svgEl('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img' }, kids));
}

/* -------------------------------------------------------------- tooltip */

function showTip(event, title, rows) {
  const tip = $('#tooltip');
  tip.replaceChildren(
    el('div', { class: 'tt-title' }, title),
    ...rows.map(([k, v]) => el('div', { class: 'tt-row' },
      el('span', {}, k), el('span', {}, String(v)))));
  tip.classList.add('on');
  const box = tip.getBoundingClientRect();
  const left = Math.min(event.clientX + 14, window.innerWidth - box.width - 10);
  const top = Math.min(event.clientY + 14, window.innerHeight - box.height - 10);
  tip.style.left = `${Math.max(6, left)}px`;
  tip.style.top = `${Math.max(6, top)}px`;
}
function hideTip() { $('#tooltip').classList.remove('on'); }

/* ----------------------------------------------------------- app wiring */

function switchView(view) {
  state.view = view;
  for (const button of document.querySelectorAll('nav.tabs button[data-view]')) {
    button.setAttribute('aria-selected', String(button.dataset.view === view));
  }
  for (const section of document.querySelectorAll('.view')) {
    section.hidden = section.id !== `view-${view}`;
  }
  if (view === 'listings') loadListings(1);
  if (view === 'market') loadMarket();
  if (view === 'changes') loadChanges();
  if (view === 'data') { renderDataView(); pollJobs(); }
}

async function saveAssumptions() {
  try {
    await api('/api/assumptions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ assumptions: state.assumptions }),
    });
    state.defaults = { ...state.assumptions };
    toast('Saved to config.yml.');
  } catch (error) {
    toast(error.message, true);
  }
}

async function bootstrap({ keepAssumptions = false } = {}) {
  const boot = await api('/api/bootstrap');
  state.boot = boot;
  if (!keepAssumptions) {
    state.assumptions = { ...boot.assumptions };
    state.defaults = { ...boot.assumptions };
    renderControls();
  }

  const s = boot.stats;
  $('#header-sub').textContent =
    `${num(s.active)} active listings · ${num(s.delisted)} delisted · ${num(s.runs)} crawls`;

  for (const select of [$('#f-town'), $('#l-town')]) {
    const current = select.value;
    select.replaceChildren(
      el('option', { value: '' }, select.id === 'f-town' ? 'Everywhere' : 'Any'),
      ...boot.towns.map((t) => el('option', { value: t }, t)));
    select.value = current;
  }
  $('#j-category').replaceChildren(...boot.categories.map((c) =>
    el('option', { value: c, selected: c === 'apartment-for-sale' ? '' : null }, c)));
  return boot;
}

function wire() {
  for (const button of document.querySelectorAll('nav.tabs button[data-view]')) {
    button.addEventListener('click', () => switchView(button.dataset.view));
  }
  $('#theme-toggle').addEventListener('click', () => {
    const now = document.documentElement.getAttribute('data-theme');
    const next = now === 'dark' ? 'light' : now === 'light' ? '' : 'dark';
    if (next) document.documentElement.setAttribute('data-theme', next);
    else document.documentElement.removeAttribute('data-theme');
    try { localStorage.setItem('jskre-theme', next); } catch { /* private mode */ }
  });
  try {
    const saved = localStorage.getItem('jskre-theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  } catch { /* ignore */ }

  for (const id of ['#f-condition', '#f-town', '#f-sort', '#f-screens']) {
    $(id).addEventListener('change', loadCandidates);
  }
  $('#save-assumptions').addEventListener('click', saveAssumptions);
  $('#reset-assumptions').addEventListener('click', () => {
    state.assumptions = { ...state.defaults };
    renderControls();
    loadCandidates();
  });
  $('#export-candidates').addEventListener('click', exportCandidates);

  $('#l-q').addEventListener('input', scheduleListings);
  for (const id of ['#l-town', '#l-active']) $(id).addEventListener('change', () => loadListings(1));
  for (const id of ['#l-min-price', '#l-max-price', '#l-min-area']) {
    $(id).addEventListener('input', scheduleListings);
  }
  $('#m-min').addEventListener('change', loadMarket);
  $('#c-days').addEventListener('change', loadChanges);

  $('#start-scrape').addEventListener('click', () => startJob('scrape'));
  $('#start-details').addEventListener('click', () => startJob('details'));
  $('#stop-job').addEventListener('click', stopJob);

  $('#drawer').addEventListener('click', (event) => {
    if (event.target === $('#drawer')) closeDrawer();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeDrawer();
  });
}

(async function main() {
  wire();
  try {
    await bootstrap();
    await loadCandidates();
    pollJobs();
  } catch (error) {
    document.querySelector('main').prepend(
      el('p', { class: 'note-box' }, `Could not reach the API: ${error.message}`));
  }
})();
