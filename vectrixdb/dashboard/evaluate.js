/* Evaluate's Retrieval tab: how every setup scored against the golden questions, read only.
   #/evaluate/retrieval is the results; #/evaluate/retrieval/<setup key> is one setup. Nothing
   here searches: a run is made by the library, `vectrixdb evaluate` or a
   function watching the golden file, and saved where the server reads it.
   Charts are drawn at the width they are shown at, so their text is the
   size it says, and drawn again when that width changes. */

const EV = { q: '', report: null, history: [], where: null, loadedAt: 0, missing: false, error: null, filter: 'all', page: 0, byKey: {}, resizeTimer: null, viewing: null, runs: [], more: false };
const EV_PER_PAGE = 10;
// Runs listed to open, a page at a time; the newest page is also what "run by run" is drawn from.
const EV_RUNS_PAGE = 12;
const EV_ENGINE_ICON = { vectrixdb: 'layers', azure: 'cloud', opensearch: 'cloud' };
const EV_NEIGHBOUR_TONES = ['n2', 'n3', 'n4'];

/* ------------------------------------------------------------ data */
function evOk(r) { return (r.setups || []).filter((s) => !s.error && s.summary && s.summary.questions); }
function evPct(x) { return x === null || x === undefined ? '–' : `${Math.round(x * 100)}%`; }
function evMs(x) { if (x === null || x === undefined) return '–'; return x >= 1000 ? `${(x / 1000).toFixed(x >= 10000 ? 0 : 1)} s` : `${x < 10 ? Number(x).toFixed(1) : Math.round(x)} ms`; }
function evTone(s) { const ms = s.models || []; return ms.length > 1 ? 'two' : ms.length ? (ms[0].kind || 'service') : 'none'; }
function evTitle(s) { return `${s.method_label} on ${s.engine}`; }
function evHref(s) { return `#/evaluate/retrieval/${encodeURIComponent(s.key)}`; }
// VectrixDB first, as the canvas lists it, then the others by name.
function evEngineOrder(a, b) { return (a === 'VectrixDB' ? 0 : 1) - (b === 'VectrixDB' ? 0 : 1) || a.localeCompare(b); }
function evGolden(r) { const g = r.golden || {}; return g.labelled ?? g.questions ?? 0; }
// The collections a run searched: recorded on the run, or read off its targets for a run saved before that.
function evCollections(r) { const kept = r.collections || []; return kept.length ? kept : Object.entries(r.targets || {}).map(([name, t]) => (t && t.collection) || name); }

function evUse(report) {
  EV.report = report; EV.byKey = {}; (report.setups || []).forEach((s) => { EV.byKey[s.key] = s; });
}

async function evFetch(force) {
  if (!force && (EV.report || EV.missing) && Date.now() - EV.loadedAt < 30000) return;
  EV.error = null; EV.missing = false;
  try {
    // The run being read: the newest, or an older one somebody opened.
    const one = (id) => api(`/api/v1/evaluations/${encodeURIComponent(id)}`, { quiet: true });
    const [report, list] = await Promise.all([one(EV.viewing || 'latest').catch((e) => { if (EV.viewing && e.status === 404) { EV.viewing = null; return one('latest'); } throw e; }), api(`/api/v1/evaluations?limit=${EV_RUNS_PAGE}`, { quiet: true })]);
    evUse(report); EV.history = (list && list.runs) || []; EV.where = list && list.where;
    EV.runs = EV.history.slice(); EV.more = EV.history.length >= EV_RUNS_PAGE;
  } catch (e) {
    EV.report = null;
    if (e.status === 404) { EV.missing = true; try { const list = await api('/api/v1/evaluations?limit=1', { quiet: true }); EV.where = list && list.where; } catch (x) { /* the list is only for the path */ } }
    else EV.error = e.message;
  }
  EV.loadedAt = Date.now();
}

/* ------------------------------------------------------------ which run */
// The newest run is what the page opens on. An older one is a click away in
// the menu at the end of the golden data line, and says so while it is open.
/* Where the answer sits on average, in words, with the mean reciprocal rank
   in the tooltip: one number over the picture of how far down it was. The
   place is 1 over the MRR, rounded, so an MRR of 0.5 reads "2nd". */
function evPlaceLine(s) {
  const mrr = s.summary && typeof s.summary.mrr === 'number' ? s.summary.mrr : null;
  if (!mrr) return '';
  const place = Math.max(1, Math.round(1 / mrr));
  const suffix = place % 100 >= 11 && place % 100 <= 13 ? 'th' : { 1: 'st', 2: 'nd', 3: 'rd' }[place % 10] || 'th';
  return `<div class="tr-headline" title="MRR ${mrr.toFixed(2)}: mean reciprocal rank, the average of 1 over the answer's position; 1 would be first every time"><span class="tr-big">${place}${suffix}</span><span class="tr-sub">the answer's place, on average</span></div>`;
}
function evRunOf(id) { return (EV.runs || []).find((h) => h.id === id) || (EV.history || []).find((h) => h.id === id); }
function evRunLine(h) {
  const pick = (h.picks || {}).finds_the_most; const setups = Object.keys(h.top10 || {}).length;
  const most = pick && h.questions && h.questions[pick] ? `finds the most ${evPct(h.top10[pick] / h.questions[pick])}` : 'no setup ran';
  const where = evCollections(h).join(', ');
  return `${where ? `${where} · ` : ''}${fmtNum(setups)} setup${setups === 1 ? '' : 's'} · ${most}`;
}
function evRunButton(r) {
  const newest = !EV.viewing;
  if (newest && (EV.runs || []).length < 2) return '';
  const label = newest ? 'Newest run' : `Run from ${ago(r.created_at)}`;
  return `<div class="menu-wrap ev-runwrap"><button class="btn sm ev-runbtn" type="button" aria-haspopup="menu" aria-expanded="false" data-on-click="[[&quot;evToggleRuns&quot;,{&quot;$&quot;:&quot;event&quot;}]]"><span>${esc(label)}</span>${icon('chevron-down', 14)}</button><div class="menu ev-runmenu" role="menu" aria-label="Runs" hidden>${evRunItems(r)}</div></div>`;
}
function evRunItems(r) {
  const rows = (EV.runs || []).map((h) => {
    const current = h.id === r.id;
    return `<button class="menu-item ev-runitem" type="button" role="menuitemradio" aria-checked="${current}" ${on('click', ['evOpenRun', h.id])}><span class="ev-col"><b>${esc(ago(h.created_at))}</b><span class="faint">${esc(evRunLine(h))}</span></span>${current ? icon('check', 16) : ''}</button>`;
  }).join('');
  return rows + (EV.more ? `<div class="menu-sep"></div><button class="menu-item ev-older" type="button" data-on-click="[[&quot;evOlderRuns&quot;,{&quot;$&quot;:&quot;event&quot;}]]">Show older runs</button>` : '');
}
function evToggleRuns(e) {
  e.stopPropagation();
  const menu = document.querySelector('.ev-runmenu'); const btn = document.querySelector('.ev-runbtn'); if (!menu || !btn) return;
  const open = menu.hidden; menu.hidden = !open; btn.setAttribute('aria-expanded', String(open));
  if (open) { const first = menu.querySelector('[aria-checked="true"]') || menu.querySelector('button'); if (first) first.focus(); }
}
function evCloseRuns(refocus) {
  const menu = document.querySelector('.ev-runmenu'); if (!menu || menu.hidden) return;
  menu.hidden = true; const btn = document.querySelector('.ev-runbtn');
  if (btn) { btn.setAttribute('aria-expanded', 'false'); if (refocus) btn.focus(); }
}
async function evOlderRuns(e) {
  e.stopPropagation();
  try {
    const list = await api(`/api/v1/evaluations?limit=${EV_RUNS_PAGE}&offset=${EV.runs.length}`, { quiet: true });
    const more = (list && list.runs) || []; EV.runs = EV.runs.concat(more); EV.more = more.length >= EV_RUNS_PAGE;
  } catch (x) { snack(x.message); return; }
  const menu = document.querySelector('.ev-runmenu'); if (menu && EV.report) menu.innerHTML = evRunItems(EV.report);
}
async function evOpenRun(id) {
  evCloseRuns();
  const newest = !id || id === ((EV.runs || [])[0] || {}).id;
  EV.viewing = newest ? null : id; EV.page = 0; EV.filter = 'all';
  if (state.evalSetup) { go('#/evaluate/retrieval'); }
  const body = $('ev-body'); if (body) body.setAttribute('aria-busy', 'true');
  await evFetch(true);
  if (body) body.removeAttribute('aria-busy');
  evRender(); window.scrollTo({ top: 0 });
}
function evOldRunNote(r) {
  if (!EV.viewing) return '';
  return `<div class="notice info ev-oldrun">${icon('history')}<div class="grow">This is the run from ${esc(ago(r.created_at))}, with ${countOf(evOk(r).length, 'setup')}.</div><a href="#/evaluate/retrieval" data-on-click="[[&quot;evOpenRun&quot;,null]]" data-prevent>Back to the newest run</a></div>`;
}
function evMissingNotes(r) {
  return (r.missing_notes || []).map((text) => `<div class="notice warn">${icon('alert')}<div>${esc(text)}</div></div>`).join('');
}
function evGoldenLine(r) {
  const download = r.golden_download
    ? `<a class="ev-dl" href="${API}/api/v1/evaluations/${encodeURIComponent(r.id)}/golden" download aria-label="Download the golden questions" title="Download the golden questions">${icon('download', 15)}</a>`
    : '';
  const where = evCollections(r).join(', ');
  return `<div class="ev-ctx"><div class="ev-golden"><span class="faint">Golden data: <b>${countOf(evGolden(r), 'question')}</b>${where ? ` · ${esc(where)}` : ''}</span>${download}</div><span class="grow"></span>${evRunButton(r)}</div>`;
}
document.addEventListener('click', (e) => { if (!e.target.closest('.ev-runwrap')) evCloseRuns(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') evCloseRuns(true); });

async function loadEvaluate() {
  const body = $('ev-body');
  if (!EV.report && !EV.missing && !EV.error) body.innerHTML = '<div class="muted">Reading the last run…</div>';
  await evFetch(false);
  evRender();
}

function evRender() {
  if (state.page !== 'evaluate' || state.evalTab !== 'retrieval') return;
  const body = $('ev-body');
  document.body.classList.toggle('ev-setup-open', !!(state.page === 'evaluate' && state.evalSetup && EV.report));
  if (EV.error) { body.innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(EV.error)}</div></div>`; return; }
  if (!EV.report) { body.innerHTML = evEmpty(); return; }
  const s = state.evalSetup ? EV.byKey[state.evalSetup] : null;
  if (state.evalSetup && !s) { body.innerHTML = `<div class="notice warn">${icon('alert')}<div>That setup is not in this run. <a href="#/evaluate/retrieval">Back to results</a></div></div>`; return; }
  body.innerHTML = s ? evSetupHtml(EV.report, s) : evResultsHtml(EV.report);
  evDraw();
}

function evEmpty() {
  return `<div class="empty ev-empty">
    <div class="glyph">${icon('gauge', 28)}</div>
    <h2>Measure every setup against your golden questions</h2>
    <div>Runs come from the library. Write the questions once and every way the collection can be searched is timed and ranked, with three picks to choose from.</div>
    <pre class="ev-code">vectrixdb golden template docs --out golden.jsonl
vectrixdb evaluate docs --golden golden.jsonl</pre>
    ${EV.where ? `<div class="faint">Runs are read from <span class="mono">${esc(evWhereLabel(EV.where))}</span>${evWhereIsLocal(EV.where) ? ' under the data folder' : ''}.</div>` : ''}
  </div>`;
}

/* The runs' folder as the page names it. The server's reply carries the
   folder as it is on the server, so other tooling can find it; a viewer of
   the dashboard is not shown the machine's own path, only the part from
   'evaluations' on, which is where the data folder keeps them. An address
   (s3://, https://) is not a path and is shown as it is. */
function evWhereIsLocal(where) { return !/^[a-z][a-z0-9+.-]*:\/\//i.test(String(where)); }
function evWhereLabel(where) {
  const text = String(where);
  if (!evWhereIsLocal(text)) return text;
  const parts = text.split(/[\\/]+/).filter(Boolean);
  const at = parts.lastIndexOf('evaluations');
  return (at >= 0 ? parts.slice(at) : parts.slice(-1)).join('/');
}

/* ------------------------------------------------------------ pieces */
function evChip(m, compact) {
  const kind = ['builtin', 'service', 'hf', 'none'].includes(m.kind) ? m.kind : 'service';
  return `<span class="ev-mchip k-${kind}${compact ? ' c' : ''}" title="${esc(m.name || m.label)}">${compact ? '' : '<i></i>'}${esc(m.label)}</span>`;
}
function evChips(s, compact) {
  const ms = s.models || [];
  if (!ms.length) return `<span class="ev-chips">${evChip({ label: 'Words only', kind: 'none', name: 'BM25' }, compact)}</span>`;
  return `<span class="ev-chips">${ms.map((m) => evChip(m, compact)).join('<span class="ev-plus">+</span>')}</span>`;
}
function evBadge(s) { return `<span class="ev-badge t-${evTone(s)}">${esc(s.rank)}</span>`; }
function evSetup2(s, big) {
  return `<span class="ev-setup"><span class="l1"><span class="nm${big ? ' big' : ''}">${esc(s.method_label)}</span></span><span class="l2">${icon(EV_ENGINE_ICON[s.engine_kind] || 'cloud', 13)}<span>${esc(s.engine_short || s.engine)}</span><span class="sep2">·</span>${evChips(s, true)}</span></span>`;
}
function evStack(s, tall) {
  const n = s.summary.questions || 1; const c = s.summary.counts;
  const first = c['1'] / n * 100; const three = (c['3'] - c['1']) / n * 100; const five = (c['5'] - c['3']) / n * 100; const ten = (c['10'] - c['5']) / n * 100;
  return `<span class="ev-stack${tall ? ' tall' : ''}" aria-hidden="true"><i class="b-first" style="width: ${first.toFixed(1)}%"></i><i class="b-top3" style="width: ${three.toFixed(1)}%"></i><i class="b-top5" style="width: ${five.toFixed(1)}%"></i><i class="b-top10" style="width: ${ten.toFixed(1)}%"></i></span>`;
}
function evKeys() {
  return `<span class="ev-keys"><span class="ev-key"><b class="b-first"></b>First</span><span class="ev-key"><b class="b-top3"></b>Top 3</span><span class="ev-key"><b class="b-top5"></b>Top 5</span><span class="ev-key"><b class="b-top10"></b>Top 10</span><span class="ev-key"><b class="b-miss"></b>Missed</span></span>`;
}
function evDelta(points, invert) {
  const v = Math.round(points);
  const cls = v === 0 ? 'flat' : (v > 0) !== !!invert ? 'up' : 'down';
  return `<b class="mono ev-d ${cls}">${v > 0 ? '+' : ''}${v}</b>`;
}

/* ------------------------------------------------------------ results */
function evPickTile(name, label, r, lead) {
  const s = EV.byKey[(r.picks || {})[name]];
  if (!s) return '';
  return `<a class="ev-pick${lead ? ' lead' : ''}" href="${evHref(s)}"><span class="l">${label}</span><span class="ev-tname">${esc(evTitle(s))}</span><span class="ev-tline">${evChips(s, false)}</span><span class="ev-tline"><span><b class="mono">${evPct(s.summary.found['10'])}</b> in the top 10 · <span class="mono">${evMs(s.summary.median_ms)}</span></span></span></a>`;
}
function evPhonePicks(r) {
  const most = EV.byKey[(r.picks || {}).finds_the_most];
  if (!most) return '';
  const row = (name, label) => { const s = EV.byKey[(r.picks || {})[name]]; return s ? `<a class="ev-mrow ev-pickrow" href="${evHref(s)}"><span class="ev-col"><span class="l">${label}</span>${evSetup2(s)}</span><span class="ev-nums"><b>${evPct(s.summary.found['10'])}</b><span class="faint">${evMs(s.summary.median_ms)}</span></span></a>` : ''; };
  return `<div class="ev-picks-phone">
    <a class="card ev-lead" href="${evHref(most)}"><span class="l">Finds the most</span>${evSetup2(most)}<span class="ev-pct3"><span><b>${evPct(most.summary.found['10'])}</b><span class="faint">in the top 10</span></span><span><b>${evPct(most.summary.found['1'])}</b><span class="faint">first</span></span><span><b>${evMs(most.summary.median_ms)}</b><span class="faint">median</span></span></span></a>
    <section class="card ev-pair">${row('best_for_balance', 'Best for balance')}${row('best_for_time', 'Best for time')}</section>
  </div>`;
}
function evFiltered(r) {
  const ok = evOk(r).sort((a, b) => a.rank - b.rank);
  const q = (EV.q || '').trim().toLowerCase();
  return (EV.filter === 'all' ? ok : ok.filter((s) => s.engine === EV.filter)).filter((s) => !q || evTitle(s).toLowerCase().includes(q));
}
function evFind(value) { EV.q = value; EV.page = 0; clearTimeout(EV.findTimer); EV.findTimer = setTimeout(() => { evRender(); const el = document.querySelector('.ev-pager .find'); if (el) { el.focus(); try { el.setSelectionRange(el.value.length, el.value.length); } catch (e) {} } }, 180); }
function evResultsHtml(r) {
  const ok = evOk(r);
  const engines = [...new Set(ok.map((s) => s.engine))].sort(evEngineOrder);
  if (EV.filter !== 'all' && !engines.includes(EV.filter)) EV.filter = 'all';
  const list = evFiltered(r);
  const pages = Math.max(1, Math.ceil(list.length / EV_PER_PAGE));
  EV.page = Math.min(Math.max(0, EV.page), pages - 1);
  const from = EV.page * EV_PER_PAGE; const shown = list.slice(from, from + EV_PER_PAGE);
  const chip = (key, label, count) => `<button type="button" class="ev-kchip${EV.filter === key ? ' on' : ''}" aria-pressed="${EV.filter === key}" ${on('click', ['evFilter', key])}>${esc(label)} ${count}</button>`;
  const failed = (r.setups || []).filter((s) => s.error);
  return `
  ${evGoldenLine(r)}
  ${evOldRunNote(r)}
  ${evMissingNotes(r)}
  <div class="ev-picks">${evPickTile('finds_the_most', 'Finds the most', r, true)}${evPickTile('best_for_balance', 'Best for balance', r)}${evPickTile('best_for_time', 'Best for time', r)}</div>
  ${evPhonePicks(r)}
  <div class="ev-plots">
    <section class="ev-plot"><div class="head"><h2>Top 10 against time</h2><span class="faint"><span class="ev-wide-only">median search, log scale · every combination</span><span class="ev-phone-only">log scale</span></span></div><div class="ev-svg" id="ev-scatter"></div>${evScatterLegend(ok)}</section>
    <section class="ev-plot"><div class="head"><h2>Found in the top k</h2><span class="faint"><span class="ev-wide-only">the numbered setups</span><span class="ev-phone-only">the top ${Math.min(4, ok.length)}</span></span></div><div class="ev-svg" id="ev-topk"></div><span class="faint">Where the lines start is the right answer first; where they end is anywhere in the top 20.</span></section>
  </div>
  <section class="card ev-ranked">
    <div class="head"><h2>Every setup, ranked</h2><span class="faint ev-count">${list.length ? `${from + 1} to ${from + shown.length} of ${list.length}` : 'None'}</span>${evKeys()}</div>
    <div class="ev-filters" role="group" aria-label="Show">${chip('all', 'All', ok.length)}${engines.map((e) => chip(e, e, ok.filter((s) => s.engine === e).length)).join('')}</div>
    <div class="ev-rows">
      <div class="ev-row hd"><span></span><span>Setup</span><span><span class="ev-desk-only">Where the answer landed</span><span class="ev-narrow-only">Where it landed</span></span><span class="num">First</span><span class="num">Top 10</span><span class="num">Median</span><span></span></div>
      ${shown.map((s) => `<a class="ev-row" href="${evHref(s)}">${evBadge(s)}${evSetup2(s)}${evStack(s)}<span class="num c-first">${evPct(s.summary.found['1'])}</span><span class="num c-top10">${evPct(s.summary.found['10'])}</span><span class="num c-ms">${evMs(s.summary.median_ms)}</span><span class="ev-go">${icon('arrow-right', 14)}</span></a>`).join('')}
    </div>
    <div class="ev-pager pager"><input class="find" type="search" placeholder="Find a setup" aria-label="Find a setup" value="${esc(EV.q || '')}" ${on('input', ['evFind', VALUE])}><span class="grow"></span><span class="faint mono where">${list.length ? `${from + 1}–${from + shown.length} of ${list.length}` : (EV.q || '').trim() ? 'none match' : '0'}</span><button class="btn icon" type="button" aria-label="Previous ${EV_PER_PAGE}" data-on-click="[[&quot;evPage&quot;,-1]]"${EV.page === 0 ? ' disabled' : ''}>${icon('chevron-left', 16)}</button><button class="btn icon" type="button" aria-label="Next ${EV_PER_PAGE}" data-on-click="[[&quot;evPage&quot;,1]]"${EV.page >= pages - 1 ? ' disabled' : ''}>${icon('chevron-right', 16)}</button></div>
    ${failed.length ? `<p class="faint ev-failed">${failed.length === 1 ? 'One setup' : `${failed.length} setups`} could not run: ${failed.map((s) => `${esc(evTitle(s))} (${esc(s.error)})`).join('; ')}.</p>` : ''}
  </section>`;
}
function evScatterLegend(ok) {
  const engines = [...new Set(ok.map((s) => s.engine_kind))].sort((a, b) => (a === 'vectrixdb' ? 0 : 1) - (b === 'vectrixdb' ? 0 : 1) || a.localeCompare(b));
  const shape = { vectrixdb: 'ci', azure: 'sq', opensearch: 'di' };
  const names = {}; ok.forEach((s) => { names[s.engine_kind] = s.engine; });
  const tones = [...new Set(ok.map(evTone))];
  const toneLabel = { none: 'Words only', two: 'Two models' };
  const single = {}; ok.forEach((s) => { if ((s.models || []).length === 1) single[s.models[0].kind || 'service'] = single[s.models[0].kind || 'service'] || s.models[0].label; });
  const order = ['none', 'service', 'builtin', 'hf', 'two'].filter((t) => tones.includes(t));
  return `<div class="ev-legend">${engines.map((e) => `<span><span class="ev-shape ${shape[e] || 'ci'}"></span>${esc(names[e])}</span>`).join('')}${order.map((t) => `<span><span class="ev-dot t-${t}"></span>${esc(toneLabel[t] || single[t] || t)}</span>`).join('')}<span><svg width="18" height="6" aria-hidden="true"><line x1="0" y1="3" x2="18" y2="3" class="ev-frontier"></line></svg>nothing beats these on both</span></div>`;
}
function evFilter(key) { EV.filter = key; EV.page = 0; evRender(); }
function evPage(delta) { EV.page += delta; evRender(); const list = document.querySelector('.ev-ranked'); if (list && list.getBoundingClientRect().top < 0) list.scrollIntoView({ block: 'start' }); }

/* ------------------------------------------------------------ one setup */
function evSetupHtml(r, s) {
  const n = s.summary.questions; const c = s.summary.counts; const ok = evOk(r);
  const models = s.models || [];
  // The chips name the models, each in its tooltip; only a setup with no vectors at all says so in words.
  const modelWords = models.length ? '' : 'no vectors: BM25 over the words';
  const best = s.rank === 1;
  const landed = s.summary.landed || [0, 0, 0, 0, 0, 0];
  // A run saved before the top 5 had a band of its own counted 4th to 10th as one.
  const old = landed.length === 5;
  const names = old ? ['First', '2nd to 3rd', '4th to 10th', '11th to 20th', 'Not in the top 20'] : ['First', '2nd to 3rd', '4th to 5th', '6th to 10th', '11th to 20th', 'Not in the top 20'];
  const bands = old ? ['b-first', 'b-top3', 'b-top10', 'b-top20', 'b-miss'] : ['b-first', 'b-top3', 'b-top5', 'b-top10', 'b-top20', 'b-miss'];
  const pct = (v) => (n ? v / n * 100 : 0);
  const rows = (s.neighbours || []).map((nb) => ({ nb, t: EV.byKey[nb.key] })).filter((x) => x.t && !x.t.error);
  return `
  <div class="ev-crumb">
    <a class="ev-back" href="#/evaluate/retrieval" aria-label="Back to results" title="Back to results">${icon('arrow-left', 16)}</a>
    ${evBadge(s)}<b class="ev-crumb-title">${esc(evTitle(s))}</b><span class="grow"></span>
    <button class="btn sm" type="button" ${on('click', ['evTry', s.key])}>${icon('search', 14)}Try in Search</button>
  </div>
  ${evOldRunNote(r)}
  <div class="ev-phone-head">${evBadge(s)}${evSetup2(s, true)}</div>
  <button class="btn ev-try-phone" type="button" ${on('click', ['evTry', s.key])}>${icon('search', 16)}Try in Search</button>
  <div class="ev-strip">
    <span><b>Engine</b><span class="ev-eng">${icon(EV_ENGINE_ICON[s.engine_kind] || 'cloud', 14)}${esc(s.engine)}</span></span><span class="sep"></span>
    <span><b>Models</b>${evChips(s, false)}${modelWords ? `<span class="faint">${esc(modelWords)}</span>` : ''}</span><span class="sep"></span>
    <span><b>Ranker</b> ${esc(s.ranker || 'none')}</span>
  </div>
  <section class="card ev-kv"><span class="k">Engine</span><span class="ev-eng">${icon(EV_ENGINE_ICON[s.engine_kind] || 'cloud', 14)}${esc(s.engine)}</span><span class="k">Models</span><span>${evChips(s, false)}</span><span class="k">Ranker</span><span>${esc(s.ranker || 'none')}</span></section>
  <div class="ev-metrics">
    <div class="ev-metric${best ? ' lead' : ''}"><span class="l">Found in the top 10</span><span class="v">${evPct(s.summary.found['10'])}</span><span class="faint">${fmtNum(c['10'])} of ${fmtNum(n)}${best ? `<span class="ev-wide-only"> · the best of ${ok.length}</span>` : ''}</span></div>
    <div class="ev-metric"><span class="l">Right answer first</span><span class="v">${evPct(s.summary.found['1'])}</span><span class="faint">${fmtNum(c['1'])} of ${fmtNum(n)}</span></div>
    <div class="ev-metric"><span class="l">Median search</span><span class="v">${evMs(s.summary.median_ms)}</span><span class="faint">p95 ${evMs(s.summary.p95_ms)}</span></div>
  </div>
  <div class="ev-detail">
    <section class="ev-plot ev-pair-l">
      <div class="ev-part ev-landed">
        <div class="head"><h2>How far down the answer was</h2><span class="faint">${countOf(n, 'question')}</span></div>
        ${evPlaceLine(s)}
        <div class="ev-bar5">${landed.map((v, i) => `<i class="${bands[i]}" style="width: ${pct(v).toFixed(1)}%"></i>`).join('')}</div>
        <div class="ev-five">${landed.map((v, i) => `<span><b class="mono">${Math.round(pct(v))}%</b>${names[i]}</span>`).join('')}</div>
        <div class="ev-legrows">${landed.map((v, i) => `<div class="ev-legrow"><i class="${bands[i]}"></i><span>${names[i]}</span><span class="num">${Math.round(pct(v))}%</span></div>`).join('')}</div>
      </div>
      <div class="ev-part ev-nb">
        <div class="head"><h2><span class="ev-desk-only">Found in the top k, against its neighbours</span><span class="ev-narrow-only">Found in the top k</span></h2><span class="faint ev-narrow-only">against its neighbours</span></div>
        <div class="ev-svg" id="ev-nb"></div>
        <div class="ev-legend"><span><span class="ev-dot t-two"></span>1 this setup</span>${rows.slice(0, 3).map((x, i) => `<span><span class="ev-dot t-${EV_NEIGHBOUR_TONES[i]}"></span>${i + 2} ${esc(x.nb.change.charAt(0).toLowerCase() + x.nb.change.slice(1))}</span>`).join('')}</div>
      </div>
    </section>
    <section class="ev-plot ev-pair-r">
      <div class="ev-part ev-time">
        <div class="head"><h2>Search time</h2><span class="faint">${n === 1 ? 'the one question' : `each of the ${fmtNum(n)} questions`}</span></div>
        <div class="ev-svg" id="ev-hist"></div>
      </div>
      <div class="ev-part ev-runs">
        <div class="head"><h2>Found in the top 10, run by run</h2><span class="faint"><span class="ev-wide-only">same questions each time</span><span class="ev-phone-only">same questions</span></span></div>
        <div class="ev-svg" id="ev-runs"></div>
      </div>
    </section>
  </div>
  ${rows.length ? `<section class="card ev-change">
    <div class="head"><h2>Change one thing</h2><span class="faint">each opens its own page</span></div>
    <div class="ev-crows">
      <div class="ev-crow hd"><span>Change one thing</span><span class="num">Top 10</span><span class="num">First</span><span class="num">Time</span><span></span></div>
      ${rows.map(({ nb, t }) => { const dTop = (t.summary.counts['10'] - c['10']) / n * 100; const dFirst = (t.summary.counts['1'] - c['1']) / n * 100; const dMs = (t.summary.median_ms || 0) - (s.summary.median_ms || 0); return `<a class="ev-crow" href="${evHref(t)}"><span class="ev-col"><b>${esc(nb.change)}</b><span class="faint">${esc(nb.detail)}</span></span><span class="num"><span class="ev-lbl">Top 10 </span>${evDelta(dTop)}</span><span class="num"><span class="ev-lbl">First </span>${evDelta(dFirst)}</span><span class="num"><span class="ev-lbl">Time </span><b class="mono ev-t">${dMs > 0 ? '+' : dMs < 0 ? '-' : ''}${evMs(Math.abs(dMs))}</b></span><span class="ev-go">${icon('arrow-right', 14)}</span></a>`; }).join('')}
    </div>
  </section>` : ''}`;
}

function evTry(key) {
  const s = EV.byKey[key]; if (!s) return;
  const target = ((EV.report || {}).targets || {})[s.target] || {};
  const mode = { keyword: 'keyword', keyword_semantic: 'keyword', dense: 'dense', ultimate: 'rerank', graph: 'rerank' }[s.method] || 'hybrid';
  go('#/search');
  setTimeout(async () => {
    const sel = $('s-collection');
    if (target.collection && [...sel.options].some((o) => o.value === target.collection)) sel.value = target.collection;
    await availableModes(); setMode(mode);
  }, 120);
}

/* ------------------------------------------------------------ charts */
function evWidth(id) { const el = $(id); return el ? Math.max(260, Math.floor(el.clientWidth)) : 0; }
function evSvg(id, W, H, label, inner) { const el = $(id); if (el) el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(label)}">${inner}</svg>`; }
function evNiceLogTicks(lo, hi, room) {
  const out = [];
  for (let e = Math.floor(Math.log10(lo)) - 1; e <= Math.ceil(Math.log10(hi)); e++) [1, 2, 5].forEach((m) => { const t = m * 10 ** e; if (t >= lo && t <= hi) out.push(t); });
  if (out.length > room) return out.filter((t) => String(t).startsWith('1'));
  return out;
}
function evTickMs(t) { return t >= 1000 ? `${t / 1000} s` : t < 1 ? `${t} ms` : `${Math.round(t)} ms`; }
function evMark(kind, x, y, big, tone, faded) {
  const cls = `ev-pt t-${tone}${faded ? ' faded' : ''}`;
  if (kind === 'azure') { const h = big ? 5.2 : 4; return `<rect x="${(x - h).toFixed(1)}" y="${(y - h).toFixed(1)}" width="${2 * h}" height="${2 * h}" rx="1.5" class="${cls}"></rect>`; }
  if (kind === 'opensearch') { const h = big ? 6.4 : 5; return `<path d="M${x.toFixed(1)} ${(y - h).toFixed(1)}L${(x + h).toFixed(1)} ${y.toFixed(1)}L${x.toFixed(1)} ${(y + h).toFixed(1)}L${(x - h).toFixed(1)} ${y.toFixed(1)}Z" class="${cls}"></path>`; }
  return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${big ? 5.2 : 4}" class="${cls}"></circle>`;
}
function evBadgeSvg(x, y, n, tone, small) {
  const r = small ? 8.5 : 9;
  return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}" class="ev-bdg t-${tone}"></circle><text x="${x.toFixed(1)}" y="${(y + 3.5).toFixed(1)}" text-anchor="middle" class="ev-bdg-n t-${tone}">${n}</text>`;
}

function evScatter(r) {
  const W = evWidth('ev-scatter'); if (!W) return;
  const phone = W < 420; const H = phone ? 230 : 250;
  const L = 42; const R = 14; const T = 14; const B = 32;
  const ok = evOk(r).filter((s) => s.summary.median_ms > 0);
  if (!ok.length) return;
  const xs = ok.map((s) => s.summary.median_ms);
  let lo = Math.log10(Math.min(...xs) / 1.6); let hi = Math.log10(Math.max(...xs) * 1.35);
  if (hi - lo < 0.6) { const mid = (hi + lo) / 2; lo = mid - 0.3; hi = mid + 0.3; }
  const ys = ok.map((s) => s.summary.found['10'] * 100);
  const spread = 100 - Math.min(...ys); const step = spread > 45 ? 10 : 5;
  const ymin = Math.max(0, Math.floor((Math.min(...ys) - 2) / step) * step);
  const X = (v) => L + (Math.log10(v) - lo) / (hi - lo) * (W - L - R);
  const Y = (v) => T + (100 - v) / (100 - ymin || 1) * (H - T - B);
  let g = '';
  for (let v = ymin; v <= 100; v += step) g += `<line x1="${L}" y1="${Y(v).toFixed(1)}" x2="${W - R}" y2="${Y(v).toFixed(1)}" class="ev-grid"></line><text x="${L - 8}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end" class="ev-tick">${v}%</text>`;
  evNiceLogTicks(10 ** lo, 10 ** hi, Math.floor((W - L - R) / 48)).forEach((t) => { const x = X(t); g += `<line x1="${x.toFixed(1)}" y1="${T}" x2="${x.toFixed(1)}" y2="${H - B + 6}" class="ev-grid2"></line><text x="${x.toFixed(1)}" y="${H - B + 20}" text-anchor="middle" class="ev-tick">${evTickMs(t)}</text>`; });
  const front = new Set(r.frontier || []);
  const line = (r.frontier || []).map((k) => EV.byKey[k]).filter((s) => s && s.summary.median_ms > 0);
  if (line.length > 1) g += `<polyline points="${line.map((s) => `${X(s.summary.median_ms).toFixed(1)},${Y(s.summary.found['10'] * 100).toFixed(1)}`).join(' ')}" class="ev-frontier"></polyline>`;
  const pts = ok.map((s) => ({ s, x: X(s.summary.median_ms), y: Y(s.summary.found['10'] * 100) }));
  [...pts].sort((a, b) => (front.has(a.s.key) ? 1 : 0) - (front.has(b.s.key) ? 1 : 0)).forEach((p) => {
    g += `<a href="${evHref(p.s)}" class="ev-plink"><title>${esc(`${p.s.rank}. ${evTitle(p.s)}, ${evPct(p.s.summary.found['10'])} in the top 10, ${evMs(p.s.summary.median_ms)}`)}</title>${evMark(p.s.engine_kind, p.x, p.y, front.has(p.s.key), evTone(p.s), !front.has(p.s.key))}</a>`;
  });
  // The numbered setups, each badge placed where it covers no other badge or point.
  const numbered = pts.filter((p) => p.s.rank && p.s.rank <= (phone ? 5 : 8)).sort((a, b) => a.s.rank - b.s.rank);
  // Every candidate spot around the point, near ones first. A spot that
  // overlaps a badge or a point is out; among the rest, the one that sits
  // on the fewest other points wins, then the nearest.
  const placed = [];
  const around = []; [16, 22, 28, 36, 46, 58, 72].forEach((d) => { for (let a = -60; a < 300; a += 30) around.push([d * Math.cos(a * Math.PI / 180), d * Math.sin(a * Math.PI / 180), d]); });
  numbered.forEach((p) => {
    let at = null; let best = Infinity;
    for (const [dx, dy, d] of around) {
      const bx = p.x + dx; const by = p.y + dy;
      if (bx < L + 10 || bx > W - R - 10 || by < T + 10 || by > H - B - 10) continue;
      if (placed.some((b) => Math.hypot(b.x - bx, b.y - by) < 20)) continue;
      if (pts.some((q) => q !== p && Math.hypot(q.x - bx, q.y - by) < 11)) continue;
      const crowd = pts.filter((q) => q !== p && Math.hypot(q.x - bx, q.y - by) < 18).length;
      const cost = d + crowd * 14;
      if (cost < best) { best = cost; at = { x: bx, y: by, d }; }
    }
    if (!at) at = { x: p.x, y: Math.max(T + 10, p.y - 16), d: 16 };
    placed.push(at);
    if (at.d > 16) { const ang = Math.atan2(at.y - p.y, at.x - p.x); g += `<line x1="${(p.x + 6 * Math.cos(ang)).toFixed(1)}" y1="${(p.y + 6 * Math.sin(ang)).toFixed(1)}" x2="${(at.x - 9.5 * Math.cos(ang)).toFixed(1)}" y2="${(at.y - 9.5 * Math.sin(ang)).toFixed(1)}" class="ev-leader"></line>`; }
    g += `<a href="${evHref(p.s)}" class="ev-plink">${evBadgeSvg(at.x, at.y, p.s.rank, evTone(p.s))}</a>`;
  });
  evSvg('ev-scatter', W, H, 'Found in the top 10 against median search time', g);
}

function evLines(id, series, label, H) {
  const W = evWidth(id); if (!W) return;
  const L = 40; const R = 34; const T = 12; const B = 28;
  const ks = ['1', '3', '5', '10', '20'];
  const vals = series.flatMap((s) => s.values);
  const ymin = Math.max(0, Math.floor((Math.min(...vals) - 5) / 10) * 10);
  const X = (i) => L + i * (W - L - R) / (ks.length - 1);
  const Y = (v) => T + (100 - v) / (100 - ymin || 1) * (H - T - B);
  let g = '';
  for (let v = ymin; v <= 100; v += 10) g += `<line x1="${L}" y1="${Y(v).toFixed(1)}" x2="${W - R}" y2="${Y(v).toFixed(1)}" class="ev-grid"></line><text x="${L - 8}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end" class="ev-tick">${v}%</text>`;
  ks.forEach((k, i) => { g += `<text x="${X(i).toFixed(1)}" y="${H - 10}" text-anchor="middle" class="ev-tick">top ${k}</text>`; });
  [...series].reverse().forEach((s) => {
    const pts = s.values.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
    g += `<polyline points="${pts}" class="ev-line ${s.tone}${s.strong ? ' strong' : ''}"></polyline>`;
    s.values.forEach((v, i) => { g += `<circle cx="${X(i).toFixed(1)}" cy="${Y(v).toFixed(1)}" r="${s.strong ? 3 : 2.4}" class="ev-dotfill ${s.tone}"></circle>`; });
  });
  const top = Y(series[0].values[ks.length - 1]);
  series.forEach((s, i) => { g += `<a href="${s.href}" class="ev-plink">${evBadgeSvg(W - R + 18, Math.min(H - B, top + i * 19), s.n, s.badge, true)}</a>`; });
  evSvg(id, W, H, label, g);
}
function evFoundK(s) { return ['1', '3', '5', '10', '20'].map((k) => (s.summary.found[k] || 0) * 100); }
function evTopK(r) {
  const ok = evOk(r).sort((a, b) => a.rank - b.rank).slice(0, 4);
  if (!ok.length) return;
  const phone = evWidth('ev-topk') < 420;
  evLines('ev-topk', ok.map((s, i) => ({ values: evFoundK(s), tone: `t-${evTone(s)}`, badge: evTone(s), strong: i === 0, n: s.rank, href: evHref(s) })), 'Found in the top k', phone ? 200 : 235);
}
function evNeighbours(s) {
  const rows = (s.neighbours || []).map((nb) => EV.byKey[nb.key]).filter((t) => t && !t.error).slice(0, 3);
  const phone = evWidth('ev-nb') < 420;
  const series = [{ values: evFoundK(s), tone: 't-two', badge: 'two', strong: true, n: 1, href: evHref(s) }]
    .concat(rows.map((t, i) => ({ values: evFoundK(t), tone: `t-${EV_NEIGHBOUR_TONES[i]}`, badge: EV_NEIGHBOUR_TONES[i], n: i + 2, href: evHref(t) })));
  evLines('ev-nb', series, 'Found in the top k, against its neighbours', phone ? 200 : 210);
}
function evNiceStep(span, count) { const raw = span / count; const mag = 10 ** Math.floor(Math.log10(raw)); return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((v) => v >= raw) || raw; }
function evHistogram(s) {
  const W = evWidth('ev-hist'); if (!W) return;
  const phone = W < 420; const H = phone ? 170 : 190;
  const L = 34; const R = 12; const T = 16; const B = 28;
  const times = [...(s.times_ms || [])].sort((a, b) => a - b); if (!times.length) return;
  const top = times[Math.max(0, Math.ceil(0.99 * times.length) - 1)];
  const step = evNiceStep(Math.max(top * 1.08, 1), phone ? 3 : 4);
  const xmax = Math.ceil(Math.max(top * 1.08, s.summary.p95_ms || 0) / step) * step;
  const bins = phone ? 18 : 23; const counts = new Array(bins).fill(0);
  times.forEach((t) => { counts[Math.min(bins - 1, Math.floor(t / xmax * bins))] += 1; });
  const peak = Math.max(...counts) || 1;
  const X = (v) => L + v / xmax * (W - L - R); const bw = (W - L - R) / bins;
  let g = `<line x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}" class="ev-axis"></line>`;
  counts.forEach((c2, i) => { const h = c2 / peak * (H - T - B - 14); g += `<rect x="${(L + i * bw + 1).toFixed(1)}" y="${(H - B - h).toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${h.toFixed(1)}" rx="2" class="ev-hbar"></rect>`; });
  // A label near the right edge is set against it, so the last one is never cut.
  for (let v = 0; v <= xmax + 1e-9; v += step) { const x = X(v); const edge = x > W - 26; g += `<text x="${(edge ? W - 2 : x).toFixed(1)}" y="${H - 10}" text-anchor="${edge ? 'end' : 'middle'}" class="ev-tick">${evTickMs(Math.round(v * 10) / 10)}</text>`; }
  const med = s.summary.median_ms; const p95 = s.summary.p95_ms;
  const mx = X(Math.min(med, xmax)); const px = X(Math.min(p95, xmax));
  const tight = px - mx < 92;
  g += `<line x1="${mx.toFixed(1)}" y1="${T}" x2="${mx.toFixed(1)}" y2="${H - B}" class="ev-mark"></line><text x="${(mx + 5).toFixed(1)}" y="${T + 10}" class="ev-mark-t">median ${evMs(med)}</text>`;
  g += `<line x1="${px.toFixed(1)}" y1="${T}" x2="${px.toFixed(1)}" y2="${H - B}" class="ev-mark"></line><text x="${(px + 5 > W - 80 ? px - 5 : px + 5).toFixed(1)}" y="${T + (tight ? 24 : 10)}" text-anchor="${px + 5 > W - 80 ? 'end' : 'start'}" class="ev-mark-t">p95 ${evMs(p95)}</text>`;
  evSvg('ev-hist', W, H, 'Search times across the questions', g);
}
function evRuns(r, s) {
  const W = evWidth('ev-runs'); if (!W) return;
  const phone = W < 420; const H = phone ? 140 : 150;
  const sha = (r.golden || {}).sha256;
  const runs = (EV.history || []).filter((h) => h.top10 && h.top10[s.key] !== undefined && (!sha || (h.golden || {}).sha256 === sha)).slice(0, phone ? 5 : 8).reverse();
  if (runs.length < 2) { $('ev-runs').innerHTML = `<p class="faint ev-one">${runs.length ? 'One run so far with these questions. The next one starts the line.' : 'No earlier runs with these questions.'}</p>`; return; }
  const vals = runs.map((h) => h.top10[s.key] / (h.questions[s.key] || 1) * 100);
  const lo = Math.max(0, Math.floor((Math.min(...vals) - 2) / 2) * 2); const hi = Math.min(100, Math.ceil((Math.max(...vals) + 2) / 2) * 2);
  const L = 40; const R = 16; const T = 16; const B = 26;
  const X = (i) => L + i * (W - L - R) / (runs.length - 1); const Y = (v) => T + (hi - v) / (hi - lo || 1) * (H - T - B);
  const every = (hi - lo) > 12 ? 4 : 2;
  let g = '';
  for (let v = lo; v <= hi; v += every) g += `<line x1="${L}" y1="${Y(v).toFixed(1)}" x2="${W - R}" y2="${Y(v).toFixed(1)}" class="ev-grid"></line><text x="${L - 8}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end" class="ev-tick">${v}%</text>`;
  // Where the index itself changed between two runs, the line says so.
  runs.forEach((h, i) => {
    if (!i) return;
    const before = JSON.stringify(runs[i - 1].targets || {}); const now = JSON.stringify(h.targets || {});
    if (before !== now) g += `<line x1="${X(i).toFixed(1)}" y1="${T}" x2="${X(i).toFixed(1)}" y2="${H - B}" class="ev-change-mark"></line><text x="${(X(i) + 5).toFixed(1)}" y="${T + 9}" class="ev-change-t">index changed</text>`;
  });
  g += `<polygon points="${X(0).toFixed(1)},${H - B} ${vals.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ')} ${X(runs.length - 1).toFixed(1)},${H - B}" class="ev-area"></polygon>`;
  g += `<polyline points="${vals.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ')}" class="ev-runline"></polyline>`;
  vals.forEach((v, i) => { const last = i === vals.length - 1; const x = X(i); const edge = x > W - 24; g += `<circle cx="${x.toFixed(1)}" cy="${Y(v).toFixed(1)}" r="${last ? 4.5 : 3.5}" class="ev-runpt${last ? ' last' : ''}"></circle><text x="${(edge ? W - 2 : x).toFixed(1)}" y="${H - 8}" text-anchor="${edge ? 'end' : 'middle'}" class="ev-tick">${esc(ago(runs[i].created_at))}</text>`; });
  g += `<text x="${(X(vals.length - 1) - 8).toFixed(1)}" y="${(Y(vals[vals.length - 1]) - 9).toFixed(1)}" text-anchor="end" class="ev-runval">${Math.round(vals[vals.length - 1])}%</text>`;
  evSvg('ev-runs', W, H, 'Found in the top 10, run by run', g);
}

function evDraw() {
  const r = EV.report; if (!r || state.page !== 'evaluate' || state.evalTab !== 'retrieval') return;
  const s = state.evalSetup ? EV.byKey[state.evalSetup] : null;
  if (s) { evNeighbours(s); evHistogram(s); evRuns(r, s); } else { evScatter(r); evTopK(r); }
}
window.addEventListener('resize', () => { clearTimeout(EV.resizeTimer); EV.resizeTimer = setTimeout(evDraw, 150); });
