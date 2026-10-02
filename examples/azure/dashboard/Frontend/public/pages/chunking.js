/* Evaluate's Chunking tab: how each way of cutting the documents answered the golden questions, read only.
   #/evaluate/chunking is the results; #/evaluate/chunking/<technique> is one technique. Nothing here
   builds anything: a run is made by the library's compare_chunking, or a function that runs it, and
   saved beside the evaluation runs. Every technique hands the model the same characters, so the counts
   compare. It borrows the Retrieval tab's parts: its tiles, rows, run menu and charts. */

const CK = { report: null, history: [], runs: [], more: false, where: null, loadedAt: 0, missing: false, error: null, viewing: null, resizeTimer: null, techPage: 0 };
const CK_RUNS_PAGE = 12;
// A colour and a shape a technique keeps on every chart, whatever its rank.
const CK_TONES = ['markdown', 'parent', 'recursive', 'sentence', 'semantic', 'fixed', 'llm'];
const CK_SHAPES = { markdown: 'circle', parent: 'square', recursive: 'diamond', sentence: 'up', semantic: 'down', fixed: 'hex', llm: 'bar' };
const CK_NAMES = { markdown: 'Structure-aware', parent: 'Parent-child', recursive: 'Recursive', sentence: 'Sentence', semantic: 'Semantic', fixed: 'Fixed-size', llm: 'LLM-based' };
// What a build put in front of each chunk for the embedder, or how it embedded it.
const CK_ADDS = { headings: 'headings embedded', none: 'headings left out', context: "the model's note in front", late: 'embedded late' };

/* ------------------------------------------------------------ data */
function ckTone(key) { return CK_TONES.includes(key) ? key : 'other'; }
function ckTechs(r) { return (r && r.techniques) || []; }
function ckByKey(r, key) { return ckTechs(r).find((t) => t.key === key) || null; }
function ckHref(t) { return `#/evaluate/chunking/${encodeURIComponent(t.key)}`; }
function ckAnswered(r) { return r.scored === 'answered'; }
function ckPct(n, of) { return of ? `${Math.round((n || 0) / of * 100)}%` : '–'; }
function ckWord(r) { return ckAnswered(r) ? 'answered right' : 'found'; }
function ckGot(r) { return ckAnswered(r) ? 'got right' : 'found'; }
function ckSize(t) { return t.parent_size ? `${fmtNum(t.size)}-character children` : `${t.key === 'semantic' ? 'up to ' : ''}${fmtNum(t.size)} characters`; }
function ckAdds(b) { return b.adds || (b.headings ? 'headings' : 'none'); }
function ckHeadings(t) { return CK_ADDS[ckAdds(t)] || CK_ADDS.none; }
// The line a build sits on in a chart of sizes: a note rides on the headings line, late on the one without.
function ckLine(b) { const a = ckAdds(b); return a === 'context' ? 'headings' : a === 'late' ? 'none' : a; }
function ckBadge(t) { return `<span class="ev-badge ck-badge k-${ckTone(t.key)}">${esc(t.rank)}</span>`; }
function ckTie() { return '<span class="ck-tie" title="Within luck of the best: these questions cannot tell the two apart">tie</span>'; }
function ckModel(r) { const m = r.model; return m && typeof m === 'object' ? m : m ? { label: String(m), name: String(m) } : null; }
function ckSearchWords(r) {
  const s = r.search || {}; const mode = String(s.mode || 'hybrid'); const m = ckModel(r);
  return `${esc(mode.charAt(0).toUpperCase() + mode.slice(1))}${s.rerank ? ', reranked' : ''}${m ? `, <span title="${esc(m.name || m.label)}">${esc(m.label)}</span>` : ''}`;
}
function ckPickTip(r) {
  return ckAnswered(r)
    ? `Each technique hands the model the same ${fmtNum(r.budget)} characters. A judge checks every answer against the golden answer. Equal counts go to the one with fewer chunks.`
    : `Each technique hands over the same ${fmtNum(r.budget)} characters. No model answered, so this counts the questions whose answer was in them. Equal counts go to fewer chunks.`;
}
function ckFoundTip(r) {
  const words = r.by_evidence || 0; const n = r.questions || 0;
  const what = words && words >= n ? 'The words that answer it were' : words ? 'The words that answer it, or its page where the golden file quotes none, were' : 'The right page was';
  return `${what} in the ${fmtNum(r.budget)} characters handed to the model`;
}

async function ckFetch(force) {
  if (!force && (CK.report || CK.missing) && Date.now() - CK.loadedAt < 30000) return;
  CK.error = null; CK.missing = false;
  try {
    const one = (id) => api(`/api/v1/chunking/${encodeURIComponent(id)}`, { quiet: true });
    const [report, list] = await Promise.all([one(CK.viewing || 'latest').catch((e) => { if (CK.viewing && e.status === 404) { CK.viewing = null; return one('latest'); } throw e; }), api(`/api/v1/chunking?limit=${CK_RUNS_PAGE}`, { quiet: true })]);
    CK.report = report; CK.history = (list && list.runs) || []; CK.where = list && list.where;
    CK.runs = CK.history.slice(); CK.more = CK.history.length >= CK_RUNS_PAGE;
  } catch (e) {
    CK.report = null;
    if (e.status === 404) { CK.missing = true; try { const list = await api('/api/v1/chunking?limit=1', { quiet: true }); CK.where = list && list.where; } catch (x) { /* the list is only for the path */ } }
    else CK.error = e.message;
  }
  CK.loadedAt = Date.now();
}

/* ------------------------------------------------------------ which run */
// The Retrieval tab's menu, opened and closed by its handlers; only what is in it is ours.
function ckRunLine(h) {
  const best = h.best && (h.techniques || {})[h.best]; const n = Object.keys(h.techniques || {}).length;
  const most = best && best.questions ? `${CK_NAMES[h.best] || h.best} ${ckPct(best.right, best.questions)}` : 'no technique ran';
  const where = (h.collections || []).join(', ');
  return `${where ? `${where} · ` : ''}${countOf(n, 'technique')} · ${most}`;
}
function ckRunButton(r) {
  const newest = !CK.viewing;
  if (newest && (CK.runs || []).length < 2) return '';
  const label = newest ? 'Newest run' : `Run from ${ago(r.created_at)}`;
  return `<div class="menu-wrap ev-runwrap"><button class="btn sm ev-runbtn" type="button" aria-haspopup="menu" aria-expanded="false" data-on-click="[[&quot;evToggleRuns&quot;,{&quot;$&quot;:&quot;event&quot;}]]"><span>${esc(label)}</span>${icon('chevron-down', 14)}</button><div class="menu ev-runmenu" role="menu" aria-label="Runs" hidden>${ckRunItems(r)}</div></div>`;
}
function ckRunItems(r) {
  const rows = (CK.runs || []).map((h) => {
    const current = h.id === r.id;
    return `<button class="menu-item ev-runitem" type="button" role="menuitemradio" aria-checked="${current}" ${on('click', ['ckOpenRun', h.id])}><span class="ev-col"><b>${esc(ago(h.created_at))}</b><span class="faint">${esc(ckRunLine(h))}</span></span>${current ? icon('check', 16) : ''}</button>`;
  }).join('');
  return rows + (CK.more ? `<div class="menu-sep"></div><button class="menu-item ev-older" type="button" data-on-click="[[&quot;ckOlderRuns&quot;,{&quot;$&quot;:&quot;event&quot;}]]">Show older runs</button>` : '');
}
async function ckOlderRuns(e) {
  e.stopPropagation();
  try {
    const list = await api(`/api/v1/chunking?limit=${CK_RUNS_PAGE}&offset=${CK.runs.length}`, { quiet: true });
    const more = (list && list.runs) || []; CK.runs = CK.runs.concat(more); CK.more = more.length >= CK_RUNS_PAGE;
  } catch (x) { snack(x.message); return; }
  const menu = document.querySelector('.ev-runmenu'); if (menu && CK.report) menu.innerHTML = ckRunItems(CK.report);
}
async function ckOpenRun(id) {
  evCloseRuns();
  const newest = !id || id === ((CK.runs || [])[0] || {}).id;
  CK.viewing = newest ? null : id;
  if (state.chunkTech) go('#/evaluate/chunking');
  const body = $('ev-body'); if (body) body.setAttribute('aria-busy', 'true');
  await ckFetch(true);
  if (body) body.removeAttribute('aria-busy');
  ckRender(); window.scrollTo({ top: 0 });
}
function ckOldRunNote(r) {
  if (!CK.viewing) return '';
  return `<div class="notice info ev-oldrun">${icon('history')}<div class="grow">This is the run from ${esc(ago(r.created_at))}, with ${countOf(ckTechs(r).length, 'technique')}.</div><a href="#/evaluate/chunking" data-on-click="[[&quot;ckOpenRun&quot;,null]]" data-prevent>Back to the newest run</a></div>`;
}
function ckGoldenLine(r) {
  const g = r.golden || {}; const n = g.labelled ?? g.questions ?? r.questions ?? 0;
  const download = r.golden_download
    ? `<a class="ev-dl" href="${API}/api/v1/chunking/${encodeURIComponent(r.id)}/golden" download aria-label="Download the golden questions" title="Download the golden questions">${icon('download', 15)}</a>`
    : '';
  const where = (r.collections || []).join(', ');
  return `<div class="ev-ctx ck-ctx"><div class="ev-golden"><span class="faint">Golden data: <b>${countOf(n, 'question')}</b>${where ? ` · ${esc(where)}` : ''}</span>${download}</div><span class="sep2 ck-sep">·</span><span class="faint ck-budget">Each technique hands the model <b>${fmtNum(r.budget)} characters</b></span><span class="grow"></span>${ckRunButton(r)}</div>`;
}

async function loadChunking() {
  const body = $('ev-body');
  if (!CK.report && !CK.missing && !CK.error) body.innerHTML = '<div class="muted">Reading the last run…</div>';
  await ckFetch(false);
  ckRender();
}

function ckRender() {
  if (state.page !== 'evaluate' || state.evalTab !== 'chunking') return;
  const body = $('ev-body');
  document.body.classList.toggle('ev-setup-open', !!(state.chunkTech && CK.report));
  if (CK.error) { body.innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(CK.error)}</div></div>`; return; }
  if (!CK.report) { body.innerHTML = ckEmpty(); return; }
  const t = state.chunkTech ? ckByKey(CK.report, state.chunkTech) : null;
  if (state.chunkTech && !t) { body.innerHTML = `<div class="notice warn">${icon('alert')}<div>That technique is not in this run. <a href="#/evaluate/chunking">Back to results</a></div></div>`; return; }
  body.innerHTML = t ? ckTechniqueHtml(CK.report, t) : ckResultsHtml(CK.report);
  ckDraw();
}

function ckEmpty() {
  return `<div class="empty ev-empty">
    <div class="glyph">${icon('scissors', 28)}</div>
    <h2>Compare the ways of cutting your documents</h2>
    <div>Runs come from the library. Each technique is built at three sizes, with headings in and out, and hands the model the same characters for every golden question. The one that answers the most is picked.</div>
    <pre class="ev-code">from vectrixdb.evaluation import ChatWriter, compare_chunking
compare_chunking(documents, "golden.jsonl", chat=ChatWriter.from_environment(), save_to="evaluations")</pre>
    ${CK.where ? `<div class="faint">Runs are read from <span class="mono">${esc(CK.where)}</span>.</div>` : ''}
  </div>`;
}

/* ------------------------------------------------------------ results */
function ckKeys(r) {
  if (!ckAnswered(r)) return '<span class="ev-keys"><span class="ev-key"><b class="b-right"></b>Found</span><span class="ev-key"><b class="b-miss"></b>Not found</span></span>';
  return '<span class="ev-keys"><span class="ev-key"><b class="b-right"></b>Answered right</span><span class="ev-key"><b class="b-wrong"></b>Found, answered wrong</span><span class="ev-key"><b class="b-miss"></b>Not found</span></span>';
}
function ckBar(r, t) {
  const n = t.questions || 1;
  const wrong = ckAnswered(r) ? `<i class="b-wrong" style="width: ${((t.wrong || 0) / n * 100).toFixed(1)}%"></i>` : '';
  return `<span class="ev-stack ck-stack" aria-hidden="true"><i class="b-right" style="width: ${((t.right || 0) / n * 100).toFixed(1)}%"></i>${wrong}</span>`;
}
function ckH2hBar(a, b, h, title) {
  return `<span class="ck-h2h"${title ? ` title="${esc(title)}"` : ''}><i class="k-${ckTone(a.key)}" style="flex: ${h.only_this}"></i><i class="k-${ckTone(b.key)}" style="flex: ${h.only_that}"></i></span>`;
}
function ckLeadTile(r, t) {
  const n = t.questions;
  const nums = ckAnswered(r)
    ? `<b class="mono">${ckPct(t.right, n)}</b> answered right · <span class="mono">${ckPct(t.found, n)}</span> found · <span class="mono">${fmtNum(t.chunks)}</span> chunks`
    : `<b class="mono">${ckPct(t.found, n)}</b> found · <span class="mono">${fmtNum(t.chunks)}</span> chunks`;
  const three = ckAnswered(r)
    ? `<span><b>${ckPct(t.right, n)}</b><span class="faint">answered right</span></span><span><b>${ckPct(t.found, n)}</b><span class="faint">found</span></span>`
    : `<span><b>${ckPct(t.found, n)}</b><span class="faint">found</span></span>`;
  return `<a class="ev-pick lead ck-tile" href="${ckHref(t)}"><span class="l">Best technique</span>
    <span class="ck-tname">${ckBadge(t)}<span class="ev-setup"><span class="l1"><span class="ev-tname">${esc(t.name)}</span></span><span class="l2 ck-pset"><span>${ckSize(t)}</span><span class="sep2">·</span><span>${ckHeadings(t)}</span></span></span></span>
    <span class="ev-tline ck-set">${ckSize(t)}<span class="sep2">·</span>${fmtNum(t.overlap)} carried over<span class="sep2">·</span>${ckHeadings(t)}</span>
    <span class="ev-tline ck-nums"><span>${nums}</span></span>
    <span class="ev-pct3 ck-pct3">${three}<span><b>${fmtNum(t.chunks)}</b><span class="faint">chunks</span></span></span>
    <span class="ev-tline ck-why" title="${esc(ckPickTip(r))}">${icon('info', 14)}${ckAnswered(r) ? 'Picked for the most right answers' : 'Picked for finding the most'}</span></a>`;
}
function ckNextTile(r, lead, next) {
  const h = (lead.against || {})[next.key] || { only_this: 0, only_that: 0, luck: true };
  const gap = (lead.right || 0) - (next.right || 0); const n = next.questions;
  const behind = gap ? `${fmtNum(gap)} behind` : next.chunks > lead.chunks ? 'level, with more chunks' : 'level';
  const said = !h.luck ? `A gap of ${fmtNum(gap)} is more than luck` : gap ? `With ${countOf(n, 'question')}, a gap of ${fmtNum(gap)} could be luck` : `Level on ${countOf(n, 'question')}, so fewer chunks decided`;
  const split = h.only_this + h.only_that;
  const tip = `They disagree on ${countOf(split, 'question')}. A ${h.only_this} to ${h.only_that} split happens by chance ${h.luck ? 'more' : 'less'} than 1 time in 20.`;
  return `<a class="ev-pick ck-tile" href="${ckHref(next)}"><span class="l">Against the next best</span>
    <span class="ck-tname">${ckBadge(next)}<span class="ev-tname">${esc(next.name)}</span>${next.tie ? ckTie() : ''}<b class="mono ck-tpct">${ckPct(next.right, n)}</b></span>
    <span class="ev-tline ck-behind"><span><b class="mono">${ckPct(next.right, n)}</b> ${ckWord(r)} · ${behind}</span></span>
    ${ckH2hBar(lead, next, h, `The ${countOf(split, 'question')} only one of the two ${ckGot(r)}`)}
    <span class="ck-h2h-l"><span><b class="mono">${fmtNum(h.only_this)}</b> only ${esc(lead.name)} ${ckGot(r)}</span><span><b class="mono">${fmtNum(h.only_that)}</b> only ${esc(next.name)} ${ckGot(r)}</span></span>
    <span class="ck-h2h-rows"><span class="ev-legrow"><i class="k-${ckTone(lead.key)}"></i><span>Only ${esc(lead.name)} ${ckGot(r)}</span><span class="num">${fmtNum(h.only_this)}</span></span><span class="ev-legrow"><i class="k-${ckTone(next.key)}"></i><span>Only ${esc(next.name)} ${ckGot(r)}</span><span class="num">${fmtNum(h.only_that)}</span></span></span>
    <span class="ev-tline ck-why" title="${esc(tip)}">${icon('info', 14)}${said}</span></a>`;
}
/* The ranked techniques ten a page: a run over every technique and size is a long list. */
function ckTechPage(techs) { const pages = Math.max(1, Math.ceil(techs.length / PAGE)); CK.techPage = Math.min(Math.max(0, CK.techPage || 0), pages - 1); return techs.slice(CK.techPage * PAGE, CK.techPage * PAGE + PAGE); }
function ckTechPager(techs) { const from = Math.min(CK.techPage || 0, Math.max(0, Math.ceil(techs.length / PAGE) - 1)) * PAGE; return pager('cktech', { find: false, total: techs.length, from, count: Math.max(0, Math.min(PAGE, techs.length - from)) }); }
PAGERS.cktech = (q, page) => { CK.techPage = page; ckRender(); };
function ckRow(r, t) {
  const n = t.questions;
  return `<a class="ck-row" href="${ckHref(t)}">${ckBadge(t)}<span class="ev-setup"><span class="l1"><span class="nm">${esc(t.name)}</span>${t.tie ? ckTie() : ''}</span><span class="l2"><span>${ckSize(t)}</span><span class="sep2 ev-desk-only">·</span><span class="ev-desk-only">${ckHeadings(t)}</span></span></span>${ckBar(r, t)}<span class="num c-right">${ckPct(t.right, n)}</span><span class="num c-found">${ckAnswered(r) ? `${ckPct(t.found, n)}<span class="ev-phone-only"> found</span>` : ''}</span><span class="num c-chunks">${fmtNum(t.chunks)}</span><span class="ev-go">${icon('arrow-right', 14)}</span></a>`;
}
function ckHeadingsNote(r) {
  const all = ckTechs(r);
  if (all.length && all.every((t) => t.headings)) return '<span class="ev-wide-only">headings embedded</span><span class="ev-phone-only">headings in</span>';
  if (all.length && all.every((t) => !t.headings)) return '<span class="ev-wide-only">headings left out</span><span class="ev-phone-only">headings out</span>';
  return '<span class="ev-wide-only">headings as each is at its best</span><span class="ev-phone-only">as at its best</span>';
}
function ckResultsHtml(r) {
  const techs = ckTechs(r);
  const failed = (r.builds || []).filter((b) => b.error);
  const n = r.questions || 0;
  const tiles = techs.length > 1 ? ckLeadTile(r, techs[0]) + ckNextTile(r, techs[0], techs[1]) : techs.length ? ckLeadTile(r, techs[0]) : '';
  return `
  ${ckGoldenLine(r)}
  ${ckOldRunNote(r)}
  ${r.left_out ? `<div class="notice info">${icon('info')}<div>${countOf(r.left_out, 'question')} left out: ${ckAnswered(r) ? 'no golden answer to check against, or ' : ''}none of their documents was in the run.</div></div>` : ''}
  <div class="ck-tiles${techs.length > 1 ? '' : ' one'}">${tiles}</div>
  <section class="card ev-ranked ck-ranked">
    <div class="head"><div class="ck-hl"><h2>Every technique, ranked</h2><span class="faint"><span class="ev-wide-only">each at its best settings</span><span class="ev-phone-only">at its best</span></span></div>${ckKeys(r)}</div>
    ${ckTechPager(techs)}
    <div class="ck-rows${ckAnswered(r) ? '' : ' found-only'}">
      <div class="ck-row hd"><span></span><span>Technique</span><span><span class="ev-desk-only">What happened to the ${fmtNum(n)} questions</span><span class="ev-narrow-only">What happened</span></span><span class="num">${ckAnswered(r) ? 'Answered' : 'Found'}</span><span class="num" title="${esc(ckFoundTip(r))}">${ckAnswered(r) ? 'Found' : ''}</span><span class="num">Chunks</span><span></span></div>
      ${ckTechPage(techs).map((t) => ckRow(r, t)).join('')}
    </div>
    ${failed.length ? `<p class="faint ev-failed">${failed.length === 1 ? 'One build' : `${failed.length} builds`} could not run: ${failed.map((b) => `${esc(b.key)} (${esc(b.error)})`).join('; ')}.</p>` : ''}
    ${Object.keys(r.skipped || {}).length ? `<p class="faint ev-failed">Not in this run: ${Object.values(r.skipped).map((why) => esc(why)).join('; ')}.</p>` : ''}
  </section>
  <div class="ck-plots${ckAnswered(r) ? '' : ' one'}">
    <section class="ev-plot"><div class="head"><h2>${ckAnswered(r) ? 'Answered right' : 'Found'} by chunk size</h2><span class="faint">${ckHeadingsNote(r)}</span></div><div class="ev-svg" id="ck-sizes"></div><div class="ev-legend" id="ck-sizes-key"></div></section>
    ${ckAnswered(r) ? '<section class="ev-plot"><div class="head"><h2>Answered against found</h2><span class="faint">every build</span></div><div class="ev-svg" id="ck-scatter"></div><div class="ev-legend" id="ck-scatter-key"></div></section>' : ''}
  </div>`;
}

/* ------------------------------------------------------------ one technique */
function ckBuildsOf(r, t) { return (r.builds || []).filter((b) => b.technique === t.key && !b.error && b.questions); }
function ckScore(r, b) { return ckAnswered(r) ? (b.answered || 0) : (b.found || 0); }
function ckScoreOf(r, b) { return (ckScore(r, b) / (b.questions || 1)) * 100; }
function ckDelta(points) {
  const v = Math.round(points);
  return v ? `<b class="mono ck-d ${v > 0 ? 'up' : 'down'}">${v > 0 ? '+' : '-'}${Math.abs(v)}</b>` : '<span class="faint">same</span>';
}
function ckCount(v) { return v ? `<span class="mono">${v > 0 ? '+' : '-'}${fmtNum(Math.abs(v))}</span>` : '<span class="faint">same</span>'; }
function ckPlain(v, what) { return v ? `${v > 0 ? '+' : '-'}${fmtNum(Math.abs(Math.round(v)))} ${what}` : `${what} the same`; }
function ckChunkWords(ratio) {
  const near = (x) => Math.abs(ratio - x) / x < 0.15;
  if (near(0.5)) return 'half the chunks, each twice as long';
  if (near(2)) return 'twice the chunks, each half as long';
  if (near(0.25)) return 'a quarter of the chunks, each four times as long';
  if (near(4)) return 'four times the chunks, each a quarter as long';
  return ratio < 1 ? 'fewer chunks, each longer' : ratio > 1 ? 'more chunks, each shorter' : 'as many chunks';
}
function ckChanges(r, t) {
  const builds = ckBuildsOf(r, t); const best = builds.find((b) => b.key === t.best); if (!best) return [];
  const out = [];
  builds.filter((b) => ckAdds(b) === ckAdds(best) && b.size !== best.size).sort((a, b) => b.size - a.size).forEach((b) => {
    out.push({ b, what: ckSize({ ...t, size: b.size }).replace(/^./, (c) => c.toUpperCase()), detail: ckChunkWords(best.chunks ? b.chunks / best.chunks : 1) });
  });
  const words = {
    headings: ['Headings embedded', 'the heading path goes in front of each chunk'],
    none: ['Headings left out', 'the chunk’s text alone is embedded'],
    context: ["The model's note in front", 'a sentence or two placing each chunk in its document'],
    late: ['Embedded late', 'each chunk read in the whole document, then pooled'],
  };
  builds.filter((b) => b.size === best.size && ckAdds(b) !== ckAdds(best)).forEach((b) => {
    const [what, detail] = words[ckAdds(b)] || words.none;
    out.push({ b, what, detail });
  });
  return out;
}
function ckVsRows(r, t) {
  return ckTechs(r).filter((o) => o.key !== t.key).map((o) => {
    const h = (t.against || {})[o.key] || { only_this: 0, only_that: 0, luck: true };
    const tip = `They disagree on ${countOf(h.only_this + h.only_that, 'question')}. A ${h.only_this} to ${h.only_that} split happens by chance ${h.luck ? 'more' : 'less'} than 1 time in 20.`;
    const verdict = h.luck
      ? `<span class="ck-luck" title="${esc(tip)}">Could be luck</span>`
      : h.only_this > h.only_that ? `<span class="ck-ahead" title="${esc(tip)}">${icon('check', 14)}Ahead</span>` : `<span class="ck-behind-v" title="${esc(tip)}">Behind</span>`;
    return `<a class="ck-vs" href="${ckHref(o)}">${ckBadge(o)}<b class="ck-vs-name">${esc(o.name)}</b><span class="ck-vs-bar"><b class="mono">${fmtNum(h.only_this)}</b>${ckH2hBar(t, o, h)}<b class="mono">${fmtNum(h.only_that)}</b></span><span class="ck-vs-v"><b class="mono ck-vs-n">${fmtNum(h.only_this)} · ${fmtNum(h.only_that)}</b>${verdict}</span></a>`;
  }).join('');
}
function ckTechniqueHtml(r, t) {
  const n = t.questions || 0; const techs = ckTechs(r); const best = t.rank === 1; const answered = ckAnswered(r);
  const parts = [[t.right, 'b-right', answered ? 'Answered right' : 'Found']];
  if (answered) parts.push([t.wrong, 'b-wrong', 'Found, answered wrong']);
  parts.push([t.missed, 'b-miss', 'Not found']);
  const pct = (v) => (n ? v / n * 100 : 0);
  const changes = ckChanges(r, t);
  const size = `${ckSize(t)}, ${fmtNum(t.overlap)} carried over`;
  const said = { headings: ['embedded', 'Embedded with the text'], none: ['left out', 'Left out'], context: ["embedded, with the model's note", "Embedded, with the model's note in front"], late: ['left out, embedded late', 'Left out; each chunk read in its document'] }[ckAdds(t)] || ['left out', 'Left out'];
  const strip = [['Cuts', esc(t.cuts.charAt(0).toLowerCase() + t.cuts.slice(1))], ['Size', size], ['Headings', esc(said[0])], ['Search', ckSearchWords(r)]];
  const kv = [['Cuts', esc(t.cuts)], ['Size', size], ['Headings', esc(said[1])], ['Search', ckSearchWords(r)]];
  const ofAll = best ? `<span class="ev-wide-only"> · the best of ${techs.length}</span>` : '';
  const found = `<div class="ev-metric${answered ? '' : best ? ' lead' : ''}"><span class="l" title="${esc(ckFoundTip(r))}">Found within ${fmtNum(r.budget)} characters</span><span class="v">${ckPct(t.found, n)}</span><span class="faint">${fmtNum(t.found)} of ${fmtNum(n)}${answered ? '' : ofAll}</span></div>`;
  const chunks = `<div class="ev-metric"><span class="l">Chunks made</span><span class="v">${fmtNum(t.chunks)}</span><span class="faint">built in ${evMs((t.build_s || 0) * 1000)}</span></div>`;
  const metrics = answered
    ? `<div class="ev-metric${best ? ' lead' : ''}"><span class="l">Answered right</span><span class="v">${ckPct(t.right, n)}</span><span class="faint">${fmtNum(t.right)} of ${fmtNum(n)}${ofAll}</span></div>${found}${chunks}`
    : `${found}${chunks}`;
  const three = `${answered ? `<span><b>${ckPct(t.right, n)}</b><span class="faint">answered right</span></span>` : ''}<span><b>${ckPct(t.found, n)}</b><span class="faint" title="${esc(ckFoundTip(r))}">found</span></span><span><b>${fmtNum(t.chunks)}</b><span class="faint">chunks</span></span>`;
  return `
  <div class="ev-crumb">
    <a class="ev-back" href="#/evaluate/chunking" aria-label="Back to results" title="Back to results">${icon('arrow-left', 16)}</a>
    ${ckBadge(t)}<b class="ev-crumb-title">${esc(t.name)}</b>
  </div>
  ${ckOldRunNote(r)}
  <div class="ev-phone-head">${ckBadge(t)}<span class="ev-setup"><span class="l1"><span class="nm big">${esc(t.name)}</span>${t.tie ? ckTie() : ''}</span><span class="l2"><span>${ckSize(t)}</span><span class="sep2">·</span><span>${ckHeadings(t)}</span></span></span></div>
  <section class="card ck-ptiles${best ? ' ev-lead' : ''}"><div class="ev-pct3">${three}</div></section>
  <div class="ev-strip ck-strip">${strip.map(([k, v]) => `<span><b>${k}</b> ${v}</span>`).join('<span class="sep"></span>')}</div>
  <section class="card ev-kv ck-kv">${kv.map(([k, v]) => `<span class="k">${k}</span><span>${v}</span>`).join('')}</section>
  <div class="ev-metrics ck-metrics${answered ? '' : ' two'}">${metrics}</div>
  <div class="ck-detail">
    <section class="ev-plot">
      <div class="ev-part">
        <div class="head"><h2>What happened to the ${fmtNum(n)} questions</h2><span class="faint ev-wide-only">at its best settings</span></div>
        <div class="ev-bar5">${parts.map(([v, cls]) => `<i class="${cls}" style="width: ${pct(v).toFixed(1)}%"></i>`).join('')}</div>
        <div class="ev-five">${parts.map(([v, , name]) => `<span><b class="mono">${Math.round(pct(v))}%</b>${name}</span>`).join('')}</div>
        <div class="ev-legrows">${parts.map(([v, cls, name]) => `<div class="ev-legrow"><i class="${cls}"></i><span>${name}</span><span class="num">${Math.round(pct(v))}%</span></div>`).join('')}</div>
      </div>
      <div class="ev-part">
        <div class="head"><h2>Its sizes, with and without headings</h2></div>
        <div class="ev-svg" id="ck-settings"></div>
        <div class="ev-legend"><span><svg width="18" height="8" aria-hidden="true"><line x1="0" y1="4" x2="18" y2="4" class="ck-line strong k-${ckTone(t.key)}"></line></svg>headings embedded</span><span><svg width="18" height="8" aria-hidden="true"><line x1="0" y1="4" x2="18" y2="4" class="ck-line off k-${ckTone(t.key)}"></line></svg>headings left out</span>${ckBuildsOf(r, t).some((b) => ckAdds(b) === 'context') ? `<span><svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">${ckMark('diamond', 5, 5, 3.2, `ck-dot k-${ckTone(t.key)}`)}</svg>the model's note</span>` : ''}${ckBuildsOf(r, t).some((b) => ckAdds(b) === 'late') ? `<span><svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">${ckMark('square', 5, 5, 3, `ck-dot hollow k-${ckTone(t.key)}`)}</svg>embedded late</span>` : ''}</div>
      </div>
    </section>
    <section class="ev-plot">
      ${techs.length > 1 ? `<div class="ev-part">
        <div class="head"><h2>Head to head</h2><span class="faint"><span class="ev-wide-only">the same ${fmtNum(n)} questions</span><span class="ev-phone-only">same ${fmtNum(n)} questions</span></span></div>
        <div class="ck-vss"><div class="ck-vs hd"><span class="ck-vs-h">Against</span><span class="ck-vs-bar"><span>only this one ${ckGot(r)}</span><span>only them</span></span><span></span></div>${ckVsRows(r, t)}</div>
      </div>` : ''}
      <div class="ev-part">
        <div class="head"><h2>${answered ? 'Answered right' : 'Found'}, run by run</h2><span class="faint ev-wide-only">same questions each time</span></div>
        <div class="ev-svg" id="ck-runs"></div>
      </div>
    </section>
  </div>
  ${changes.length ? `<section class="card ck-change">
    <div class="head"><h2>Change one thing</h2><span class="faint"><span class="ev-wide-only">from its best settings</span><span class="ev-phone-only">from its best</span></span></div>
    <div class="ck-crows${answered ? '' : ' found-only'}">
      <div class="ck-crow hd"><span>Change one thing</span><span class="num">${answered ? 'Answered' : 'Found'}</span><span class="num">${answered ? 'Found' : ''}</span><span class="num">Chunks</span></div>
      ${changes.map(({ b, what, detail }) => {
        const dScore = (ckScore(r, b) - t.right) / (n || 1) * 100; const dFound = ((b.found || 0) - (t.found || 0)) / (n || 1) * 100; const dChunks = (b.chunks || 0) - (t.chunks || 0);
        const phone = [answered ? ckPlain(dFound, 'found') : null, ckPlain(dChunks, 'chunks')].filter(Boolean).join(' · ');
        return `<div class="ck-crow"><span class="ev-col"><b>${esc(what)}</b><span class="faint">${esc(detail)}</span></span><span class="num c-a">${ckDelta(dScore)}</span><span class="num c-f">${answered ? ckDelta(dFound) : ''}</span><span class="num c-n">${ckCount(dChunks)}</span><span class="faint ck-crow-m">${esc(phone)}</span></div>`;
      }).join('')}
    </div>
  </section>` : ''}`;
}

/* ------------------------------------------------------------ charts */
function ckMark(kind, x, y, r, cls) {
  if (kind === 'square') return `<rect x="${(x - r).toFixed(1)}" y="${(y - r).toFixed(1)}" width="${(2 * r).toFixed(1)}" height="${(2 * r).toFixed(1)}" rx="1.5" class="${cls}"></rect>`;
  const pts = kind === 'diamond' ? [[x, y - r * 1.3], [x + r * 1.3, y], [x, y + r * 1.3], [x - r * 1.3, y]]
    : kind === 'up' ? [[x, y - r * 1.25], [x + r * 1.2, y + r * 0.9], [x - r * 1.2, y + r * 0.9]]
      : kind === 'down' ? [[x, y + r * 1.25], [x + r * 1.2, y - r * 0.9], [x - r * 1.2, y - r * 0.9]]
      : kind === 'hex' ? [0, 1, 2, 3, 4, 5].map((i) => [x + r * 1.15 * Math.cos(Math.PI / 3 * i), y + r * 1.15 * Math.sin(Math.PI / 3 * i)])
        : kind === 'bar' ? [[x - r * 1.4, y - r * 0.6], [x + r * 1.4, y - r * 0.6], [x + r * 1.4, y + r * 0.6], [x - r * 1.4, y + r * 0.6]] : null;
  if (pts) return `<polygon points="${pts.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join(' ')}" class="${cls}"></polygon>`;
  return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}" class="${cls}"></circle>`;
}
function ckBadgeSvg(x, y, t, small) {
  const r = small ? 8 : 9;
  return `<a href="${ckHref(t)}" class="ev-plink"><title>${esc(`${t.rank}. ${t.name}`)}</title><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}" class="ck-bdg k-${ckTone(t.key)}"></circle><text x="${x.toFixed(1)}" y="${(y + 3.5).toFixed(1)}" text-anchor="middle" class="ck-bdg-n k-${ckTone(t.key)}">${t.rank}</text></a>`;
}
// Badges that would sit on one another are pushed apart, each on a leader to its point.
function ckSpread(ys, gap, lo, hi) {
  const order = ys.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y);
  order.forEach((e, j) => { e.p = j ? Math.max(e.y, order[j - 1].p + gap) : e.y; });
  const mean = (a) => a.reduce((s, v) => s + v, 0) / (a.length || 1);
  const shift = mean(order.map((e) => e.y)) - mean(order.map((e) => e.p)); order.forEach((e) => { e.p += shift; });
  const top = order.length ? order[0].p : 0; const bottom = order.length ? order[order.length - 1].p : 0;
  if (top < lo) order.forEach((e) => { e.p += lo - top; }); else if (bottom > hi) order.forEach((e) => { e.p -= bottom - hi; });
  const out = []; order.forEach((e) => { out[e.i] = e.p; }); return out;
}
function ckRange(vals, pad, step) {
  const lo = Math.max(0, Math.floor((Math.min(...vals) - pad) / step) * step);
  const hi = Math.min(100, Math.ceil((Math.max(...vals) + pad) / step) * step);
  return hi > lo ? [lo, hi] : [Math.max(0, lo - step), Math.min(100, hi + step)];
}
function ckSizes(r) {
  const W = evWidth('ck-sizes'); if (!W) return;
  const phone = W < 420; const H = phone ? 220 : 250;
  const L = 40; const R = 22; const T = 12; const B = 28;
  const series = ckTechs(r).map((t) => ({ t, pts: ckBuildsOf(r, t).filter((b) => ckAdds(b) === ckLine(t)).sort((a, b) => a.size - b.size) })).filter((s) => s.pts.length);
  if (!series.length) return;
  const sizes = [...new Set(series.flatMap((s) => s.pts.map((b) => b.size)))].sort((a, b) => a - b);
  const vals = series.flatMap((s) => s.pts.map((b) => ckScoreOf(r, b)));
  const step = Math.max(...vals) - Math.min(...vals) > 40 ? 20 : 10;
  const [lo, hi] = ckRange(vals, 2, step);
  const x0 = Math.log2(sizes[0]); const x1 = Math.log2(sizes[sizes.length - 1]);
  const X = (s) => (x1 > x0 ? L + (Math.log2(s) - x0) / (x1 - x0) * (W - L - R) : (L + W - R) / 2);
  const Y = (v) => T + (hi - v) / (hi - lo) * (H - T - B);
  let g = '';
  for (let v = lo; v <= hi; v += step) g += `<line x1="${L}" y1="${Y(v).toFixed(1)}" x2="${W - R}" y2="${Y(v).toFixed(1)}" class="ev-grid"></line><text x="${L - 8}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end" class="ev-tick">${v}%</text>`;
  sizes.forEach((s) => { g += `<line x1="${X(s).toFixed(1)}" y1="${T}" x2="${X(s).toFixed(1)}" y2="${H - B}" class="ev-grid2"></line><text x="${X(s).toFixed(1)}" y="${H - B + 18}" text-anchor="middle" class="ev-tick">${fmtNum(s)}</text>`; });
  [...series].reverse().forEach(({ t, pts }) => {
    g += `<polyline points="${pts.map((b) => `${X(b.size).toFixed(1)},${Y(ckScoreOf(r, b)).toFixed(1)}`).join(' ')}" class="ck-line k-${ckTone(t.key)}${t.rank === 1 ? ' strong' : ''}"></polyline>`;
    pts.forEach((b) => { g += `<circle cx="${X(b.size).toFixed(1)}" cy="${Y(ckScoreOf(r, b)).toFixed(1)}" r="2.6" class="ck-dot k-${ckTone(t.key)}"></circle>`; });
  });
  // Each technique's number on its best build.
  // A best that is a note or late sits off its line, at its own value.
  const bests = series.map(({ t, pts }) => { const b = ckBuildsOf(r, t).find((p) => p.key === t.best) || pts[pts.length - 1]; return { t, x: X(b.size), y: Y(ckScoreOf(r, b)) }; });
  const byX = {}; bests.forEach((p) => { (byX[p.x.toFixed(0)] = byX[p.x.toFixed(0)] || []).push(p); });
  Object.values(byX).forEach((group) => {
    const ys = ckSpread(group.map((p) => p.y), phone ? 17 : 19, T + 9, H - B - 9);
    group.forEach((p, i) => { p.by = ys[i]; p.moved = Math.abs(ys[i] - p.y) > 1; if (p.moved) g += `<line x1="${p.x.toFixed(1)}" y1="${p.y.toFixed(1)}" x2="${(p.x + 12).toFixed(1)}" y2="${ys[i].toFixed(1)}" class="ev-leader"></line>`; });
  });
  [...bests].reverse().forEach((p) => { g += ckBadgeSvg(Math.min(W - 10, p.moved ? p.x + 12 : p.x), p.by, p.t, phone); });
  evSvg('ck-sizes', W, H, `${ckAnswered(r) ? 'Answered right' : 'Found'} by chunk size, one line a technique`, g);
  const key = $('ck-sizes-key'); if (key) key.innerHTML = series.map(({ t }) => `<span><svg width="16" height="8" aria-hidden="true"><line x1="0" y1="4" x2="16" y2="4" class="ck-line strong k-${ckTone(t.key)}"></line></svg>${t.rank} ${esc(t.name)}</span>`).join('');
}
function ckScatter(r) {
  const W = evWidth('ck-scatter'); if (!W) return;
  const phone = W < 420; const H = phone ? 200 : 250;
  const L = 40; const R = 16; const T = 12; const B = 28;
  const builds = (r.builds || []).filter((b) => !b.error && b.questions && b.answered !== null && b.answered !== undefined);
  if (!builds.length) return;
  const fx = builds.map((b) => b.found / b.questions * 100); const ay = builds.map((b) => b.answered / b.questions * 100);
  const xstep = Math.max(...fx) - Math.min(...fx) > 25 ? 10 : 5;
  const [flo, fhi] = ckRange(fx, 2, xstep);
  const ystep = Math.max(...ay) - Math.min(...ay) > 45 ? 20 : 10;
  const alo = Math.max(0, Math.floor((Math.min(...ay) - 2) / ystep) * ystep); const ahi = Math.max(fhi, Math.ceil((Math.max(...ay) + 2) / ystep) * ystep);
  const X = (v) => L + (v - flo) / (fhi - flo) * (W - L - R); const Y = (v) => T + (ahi - v) / (ahi - alo || 1) * (H - T - B);
  let g = '';
  for (let v = alo; v <= ahi; v += ystep) g += `<line x1="${L}" y1="${Y(v).toFixed(1)}" x2="${W - R}" y2="${Y(v).toFixed(1)}" class="ev-grid"></line><text x="${L - 8}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end" class="ev-tick">${v}%</text>`;
  for (let v = flo; v <= fhi; v += xstep) g += `<line x1="${X(v).toFixed(1)}" y1="${T}" x2="${X(v).toFixed(1)}" y2="${H - B}" class="ev-grid2"></line><text x="${X(v).toFixed(1)}" y="${H - B + 18}" text-anchor="middle" class="ev-tick">${v}%</text>`;
  // Answered all it found: where answered equals found, inside the plot.
  const d0 = Math.max(flo, alo); const d1 = Math.min(fhi, ahi);
  if (d1 > d0) g += `<line x1="${X(d0).toFixed(1)}" y1="${Y(d0).toFixed(1)}" x2="${X(d1).toFixed(1)}" y2="${Y(d1).toFixed(1)}" class="ck-diag"></line>`;
  const techs = ckTechs(r); const bestKeys = new Set(techs.map((t) => t.best)); const rad = phone ? 8 : 9;
  builds.filter((b) => !bestKeys.has(b.key)).forEach((b) => { g += ckMark(CK_SHAPES[b.technique] || 'circle', X(b.found / b.questions * 100), Y(b.answered / b.questions * 100), 3.4, `ck-dot faded k-${ckTone(b.technique)}`); });
  // From where answering all it found would sit down to the badge: the found that were answered wrong.
  techs.forEach((t) => { if (!t.questions) return; const x = X(t.found / t.questions * 100); const from = Y(t.found / t.questions * 100); const to = Y(t.right / t.questions * 100) - rad; if (to > from) g += `<line x1="${x.toFixed(1)}" y1="${from.toFixed(1)}" x2="${x.toFixed(1)}" y2="${to.toFixed(1)}" class="ck-drop"></line>`; });
  [...techs].reverse().forEach((t) => { if (t.questions) g += ckBadgeSvg(X(t.found / t.questions * 100), Y(t.right / t.questions * 100), t, phone); });
  evSvg('ck-scatter', W, H, 'Answered right against found, every build, each technique at its best numbered', g);
  const key = $('ck-scatter-key');
  if (key) key.innerHTML = techs.map((t) => `<span><svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">${ckMark(CK_SHAPES[t.key] || 'circle', 5, 5, 3.4, `ck-dot k-${ckTone(t.key)}`)}</svg>${t.rank} ${esc(t.name)}</span>`).join('')
    + '<span><svg width="18" height="6" aria-hidden="true"><line x1="0" y1="3" x2="18" y2="3" class="ck-diag"></line></svg>answered all it found</span><span><svg width="8" height="12" aria-hidden="true"><line x1="4" y1="0" x2="4" y2="12" class="ck-drop"></line></svg>found, answered wrong</span>';
}
function ckSettings(r, t) {
  const W = evWidth('ck-settings'); if (!W) return;
  const phone = W < 420; const H = phone ? 200 : 230;
  const L = 40; const R = 24; const T = 20; const B = 28;
  const builds = ckBuildsOf(r, t); if (!builds.length) return;
  const sizes = [...new Set(builds.map((b) => b.size))].sort((a, b) => a - b);
  const [lo, hi] = ckRange(builds.map((b) => ckScoreOf(r, b)), 4, 10);
  const x0 = Math.log2(sizes[0]); const x1 = Math.log2(sizes[sizes.length - 1]);
  const X = (s) => (x1 > x0 ? L + (Math.log2(s) - x0) / (x1 - x0) * (W - L - R) : (L + W - R) / 2);
  const Y = (v) => T + (hi - v) / (hi - lo) * (H - T - B);
  let g = '';
  for (let v = lo; v <= hi; v += 10) g += `<line x1="${L}" y1="${Y(v).toFixed(1)}" x2="${W - R}" y2="${Y(v).toFixed(1)}" class="ev-grid"></line><text x="${L - 8}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end" class="ev-tick">${v}%</text>`;
  sizes.forEach((s) => { g += `<text x="${X(s).toFixed(1)}" y="${H - B + 18}" text-anchor="middle" class="ev-tick">${fmtNum(s)}</text>`; });
  const lines = [false, true].map((heading) => ({ heading, pts: builds.filter((b) => ckAdds(b) === (heading ? 'headings' : 'none')).sort((a, b) => a.size - b.size) }));
  lines.forEach(({ heading, pts }) => { if (pts.length) g += `<polyline points="${pts.map((b) => `${X(b.size).toFixed(1)},${Y(ckScoreOf(r, b)).toFixed(1)}`).join(' ')}" class="ck-line k-${ckTone(t.key)} ${heading ? 'strong' : 'off'}"></polyline>`; });
  // The model's note sits just right of its size and late just left, so neither covers the lines.
  const nudge = (b) => ({ context: 14, late: -14 }[ckAdds(b)] || 0);
  const best = builds.find((b) => b.key === t.best);
  if (best) g += `<circle cx="${(X(best.size) + nudge(best)).toFixed(1)}" cy="${Y(ckScoreOf(r, best)).toFixed(1)}" r="8" class="ck-ring"></circle>`;
  lines.forEach(({ heading, pts }) => pts.forEach((b) => {
    const x = X(b.size); const y = Y(ckScoreOf(r, b));
    g += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${heading ? 3.4 : 3}" class="ck-dot k-${ckTone(t.key)}${heading ? '' : ' hollow'}"></circle>`;
    g += `<text x="${x.toFixed(1)}" y="${(heading ? y - 13 : y + 17).toFixed(1)}" text-anchor="middle" class="ck-val${heading ? '' : ' off'}">${Math.round(ckScoreOf(r, b))}%</text>`;
  }));
  // The model's note and late, built at the middle size only, are marks of their own beside it.
  builds.filter((b) => ['context', 'late'].includes(ckAdds(b))).forEach((b) => {
    const note = ckAdds(b) === 'context'; const x = X(b.size) + nudge(b); const y = Y(ckScoreOf(r, b));
    g += ckMark(note ? 'diamond' : 'square', x, y, note ? 3.6 : 3.2, `ck-dot k-${ckTone(t.key)}${note ? '' : ' hollow'}`);
    g += `<text x="${(x + (note ? 8 : -8)).toFixed(1)}" y="${(y + 3.5).toFixed(1)}" text-anchor="${note ? 'start' : 'end'}" class="ck-val">${Math.round(ckScoreOf(r, b))}%</text>`;
  });
  evSvg('ck-settings', W, H, `${t.name} at each size, with and without headings`, g);
}
function ckRuns(r, t) {
  const W = evWidth('ck-runs'); if (!W) return;
  const phone = W < 420; const H = 140;
  const sha = (r.golden || {}).sha256;
  const runs = (CK.history || []).filter((h) => (h.techniques || {})[t.key] && (!sha || (h.golden || {}).sha256 === sha)).slice(0, phone ? 5 : 8).reverse();
  if (runs.length < 2) { $('ck-runs').innerHTML = `<p class="faint ev-one">${runs.length ? 'One run so far with these questions. The next one starts the line.' : 'No earlier runs with these questions.'}</p>`; return; }
  const vals = runs.map((h) => { const x = h.techniques[t.key]; return (x.right || 0) / (x.questions || 1) * 100; });
  const every = Math.max(...vals) - Math.min(...vals) > 12 ? 10 : 5;
  const [lo, hi] = ckRange(vals, 1, every);
  const L = 40; const R = 30; const T = 22; const B = 26;
  const X = (i) => L + i * (W - L - R) / (runs.length - 1); const Y = (v) => T + (hi - v) / (hi - lo) * (H - T - B);
  let g = '';
  for (let v = lo; v <= hi; v += every) g += `<line x1="${L}" y1="${Y(v).toFixed(1)}" x2="${W - R}" y2="${Y(v).toFixed(1)}" class="ev-grid"></line><text x="${L - 8}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end" class="ev-tick">${v}%</text>`;
  g += `<polyline points="${vals.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ')}" class="ck-line strong k-${ckTone(t.key)}"></polyline>`;
  vals.forEach((v, i) => { const last = i === vals.length - 1; g += `<circle cx="${X(i).toFixed(1)}" cy="${Y(v).toFixed(1)}" r="${last ? 4.5 : 3}" class="ck-dot ringed k-${ckTone(t.key)}"></circle><text x="${X(i).toFixed(1)}" y="${H - B + 17}" text-anchor="middle" class="ev-tick">${esc(ago(runs[i].created_at))}</text>`; });
  g += `<text x="${X(vals.length - 1).toFixed(1)}" y="${(Y(vals[vals.length - 1]) - 11).toFixed(1)}" text-anchor="middle" class="ck-val">${Math.round(vals[vals.length - 1])}%</text>`;
  evSvg('ck-runs', W, H, `${ckAnswered(r) ? 'Answered right' : 'Found'}, run by run, on the same questions`, g);
}

function ckDraw() {
  const r = CK.report; if (!r || state.page !== 'evaluate' || state.evalTab !== 'chunking') return;
  const t = state.chunkTech ? ckByKey(r, state.chunkTech) : null;
  if (t) { ckSettings(r, t); ckRuns(r, t); } else { ckSizes(r); if (ckAnswered(r)) ckScatter(r); }
}
window.addEventListener('resize', () => { clearTimeout(CK.resizeTimer); CK.resizeTimer = setTimeout(ckDraw, 150); });
