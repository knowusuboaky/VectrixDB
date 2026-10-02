/* The flows this app adds to the dashboard: making a collection the way the
 * app makes one, adding files to it, and deleting it.
 *
 * Each talks to the function app's own routes. /setup puts the collection's
 * record and its policy in place before a single file is read; /files drops
 * each file in raw/<name>/ for Event Grid to notice; /setup/status says where
 * the collection is, which is what the spinner reads; /delete takes
 * everything, and keeps no copy. The policy editor itself is the library's,
 * in app.js: this page only holds one on its dialog.
 *
 * Author: Kwadwo Daddy Nyame Owusu - Boakye
 */

['openCreate', 'pickCcMode', 'ccDropFile', 'createCollection', 'openFiles', 'uploadFiles', 'afDropFile', 'confirmDelete', 'deleteEverything', 'retryFailed', 'dropFailed'].forEach((name) => ACTIONS.add(name));

PAGES.push('setup'); TITLES.setup = 'Setting up';

/* ------------------------------------------------------- new collection */
let ccFiles = [];
let ccMode = 'up';
function openCreate() {
  ccFiles = []; WHO.cc = newWho(null);
  $('cc-name').value = ''; $('cc-title').value = ''; $('cc-text').value = ''; $('cc-error').textContent = '';
  pickCcMode('up'); renderWho('cc'); renderFiles('cc-files', ccFiles, 'ccDropFile');
  openDialog('dlg-create');
}
function pickCcMode(mode) {
  ccMode = mode;
  document.querySelectorAll('#cc-modes button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
  $('cc-up').hidden = mode !== 'up'; $('cc-type').hidden = mode !== 'type';
}
function renderFiles(id, files, drop) {
  $(id).innerHTML = files.map((f, i) => `<div class="tr t-files"><span>${esc(f.name)}</span><span class="faint">${fmtBytes(f.size)}</span><button class="btn sm" aria-label="Remove" ${on('click', [drop, i])}>${icon('x', 12)}</button></div>`).join('');
}
function ccDropFile(i) { ccFiles.splice(i, 1); renderFiles('cc-files', ccFiles, 'ccDropFile'); }
function wireDrop(zoneId, inputId, take) {
  const z = $(zoneId); if (!z) return;
  ['dragenter', 'dragover'].forEach((ev) => z.addEventListener(ev, (e) => { e.preventDefault(); z.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => z.addEventListener(ev, (e) => { e.preventDefault(); z.classList.remove('over'); }));
  z.addEventListener('drop', (e) => take(e.dataTransfer.files));
  $(inputId).addEventListener('change', (e) => { take(e.target.files); e.target.value = ''; });
}
function sendFile(name, file) {
  return api(`/api/v1/collections/${encodeURIComponent(name)}/files`, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name) } });
}
async function createCollection() {
  const name = $('cc-name').value.trim();
  const say = (m) => { $('cc-error').textContent = m; };
  if (!name) return say('Give the collection a name');
  if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(name)) return say('Lowercase letters, digits and dashes: it names the folder and the search index');
  let policy;
  try { policy = policyFrom(WHO.cc); } catch (e) { return say(e.message); }
  const typed = ccMode === 'type' ? $('cc-text').value.trim() : '';
  if (ccMode === 'up' && !ccFiles.length) return say('Drop a file or two, or type the text');
  if (ccMode === 'type' && !typed) return say('Type or paste the text to index');
  say('');
  try {
    await api(`/api/v1/collections/${encodeURIComponent(name)}/setup`, { method: 'POST', body: JSON.stringify({ policy }) });
    if (ccMode === 'type') await api(`/api/v1/collections/${encodeURIComponent(name)}/files`, { method: 'POST', body: JSON.stringify({ title: $('cc-title').value.trim() || null, text: typed }) });
    else for (const f of ccFiles) await sendFile(name, f);
    closeDialog('dlg-create');
    state.collections = []; state.policies = null; delete state.health[name];
    go(`#/collections/${encodeURIComponent(name)}/setup`);
  } catch (e) { say(e.message); }
}

/* ------------------------------------------------------------ add files */
let afFiles = [];
function openFiles() {
  afFiles = []; renderFiles('af-files', afFiles, 'afDropFile');
  const name = state.collection; const v = state.policies && state.policies.collections[name];
  $('af-name').textContent = name;
  $('af-policy').innerHTML = `${icon('lock', 16)}<div>Only <b>${esc(describePolicy(v && v.policy))}</b> can retrieve from these. A file follows the collection's policy and carries none of its own.</div>`;
  $('af-error').textContent = '';
  openDialog('dlg-files');
}
function afDropFile(i) { afFiles.splice(i, 1); renderFiles('af-files', afFiles, 'afDropFile'); }
async function uploadFiles() {
  const name = state.collection;
  if (!afFiles.length) { $('af-error').textContent = 'Drop a file or two first'; return; }
  try {
    for (const f of afFiles) await sendFile(name, f);
    closeDialog('dlg-files'); delete state.health[name];
    go(`#/collections/${encodeURIComponent(name)}/setup`);
  } catch (e) { $('af-error').textContent = e.message; }
}

/* ----------------------------------------------------------- the spinner */
/* One spinner in the middle of the page, its words changing with each step,
   read from /setup/status every few seconds. When the collection is ready
   the spinner becomes a tick and two buttons. Deleting uses the same page. */
function stage(title, line, { busy = true, actions = '' } = {}) {
  $('su-mark').className = busy ? 'spin' : 'done-mark';
  $('su-mark').innerHTML = busy ? '' : icon('check', 24);
  $('su-title').innerHTML = `${esc(title)}${busy ? '<span class="dots" aria-hidden="true"><i>.</i><i>.</i><i>.</i></span>' : ''}`;
  $('su-line').textContent = line;
  $('su-note').hidden = !busy;
  $('su-actions').innerHTML = actions; $('su-actions').hidden = !actions;
}
async function loadSetup() {
  clearInterval(state.setupTimer);
  const name = state.collection;
  if (state.deleting === name) return;
  const enc = encodeURIComponent(name);
  const tick = async () => {
    if (state.page !== 'setup' || state.collection !== name) { clearInterval(state.setupTimer); return; }
    let d;
    try { d = await api(`/api/v1/collections/${enc}/setup/status`, { quiet: true }); } catch (e) { clearInterval(state.setupTimer); stage(name, e.message, { busy: false, actions: `<button class="btn" ${on('click', ['go', '#/collections'])}>Back to collections</button>` }); return; }
    if (d.ready) {
      clearInterval(state.setupTimer);
      state.collections = []; delete state.health[name];
      stage(`${name} is ready`, d.line, { busy: false, actions: `${can('search') ? `<button class="btn primary" ${on('click', ['searchIn', name])}>${icon('search', 15)} Search it</button>` : ''}<button class="btn" ${on('click', ['go', '#/collections/' + enc])}>Open collection</button>` });
      return;
    }
    if (d.failed) {
      clearInterval(state.setupTimer);
      stage(d.step, d.line, { busy: false, actions: `<button class="btn" ${on('click', ['go', '#/evaluate'])}>Open Evaluate</button><button class="btn" ${on('click', ['go', '#/collections/' + enc])}>Open collection</button>` });
      return;
    }
    stage(d.step, d.line);
  };
  stage('Policy', 'Attaching who can retrieve from it, before a single file is read.');
  await tick();
  state.setupTimer = setInterval(tick, 3000);
}

/* --------------------------------------------------------------- delete */
function confirmDelete(name) {
  state.deleteName = name;
  $('del-name').textContent = name; $('del-typed').value = ''; $('del-error').textContent = '';
  openDialog('dlg-delete');
}
async function deleteEverything() {
  const name = state.deleteName;
  if (!name) return;
  if ($('del-typed').value.trim() !== name) { $('del-error').textContent = 'Type the name exactly to confirm'; return; }
  closeDialog('dlg-delete');
  state.deleting = name; state.collection = name;
  go(`#/collections/${encodeURIComponent(name)}/setup`);
  stage('New files refused', `Nothing lands in raw/${name}/ while it is being deleted.`);
  let d;
  try { d = await api(`/api/v1/collections/${encodeURIComponent(name)}/delete`, { method: 'POST' }); }
  catch (e) { state.deleting = null; stage('Not deleted', e.message, { busy: false, actions: `<button class="btn" ${on('click', ['go', '#/collections'])}>Back to collections</button>` }); return; }
  const steps = d.steps || [];
  let i = 0;
  const next = () => {
    if (state.page !== 'setup' || state.collection !== name) { state.deleting = null; return; }
    if (i >= steps.length) {
      state.deleting = null; state.collections = []; state.policies = null; delete state.health[name];
      stage(`${name} is deleted`, 'Nothing of it is left, and the name can be used again.', { busy: false, actions: `<button class="btn" ${on('click', ['go', '#/collections'])}>Back to collections</button>` });
      return;
    }
    stage(steps[i].step, steps[i].line); i += 1; setTimeout(next, 1400);
  };
  next();
}

document.addEventListener('DOMContentLoaded', () => {
  wireDrop('cc-drop', 'cc-input', (files) => { ccFiles.push(...files); renderFiles('cc-files', ccFiles, 'ccDropFile'); });
  wireDrop('af-drop', 'af-input', (files) => { afFiles.push(...files); renderFiles('af-files', afFiles, 'afDropFile'); });
  window.addEventListener('hashchange', () => clearInterval(state.setupTimer));
});

/* ------------------------------------------------------ would not read */
/* The poison queue, in words: one row a file the worker could not read, ten
   a page, with Retry and Drop. Its count sits on the Overview's Needs
   attention card. The rows come from the app's own route, which joins the
   queue with the record the worker keeps of each failure. For whoever may
   write: a viewer sees neither the card nor the count. */
async function loadPoisonCount() {
  if (!canWrite()) { state.poison = null; state.ingestion = null; return; }
  try { const d = await api('/api/v1/ingest/failed?limit=1', { quiet: true }); state.poison = { total: d.total || 0 }; } catch (e) { state.poison = null; }
  await loadIngestState();
}
function poisonAttention() {
  const out = [];
  if (state.ingestion && state.ingestion.paused) out.push({ kind: 'warn', icon: 'alert', html: `${pausedLine(state.ingestion)} <a href="#/ingest">See Ingest</a>` });
  const n = state.poison && state.poison.total;
  if (n) out.push({ kind: 'warn', icon: 'alert', html: `<b>${countOf(n, 'file')} would not read.</b> Each tried 5 times. <a href="#/ingest">See them on Ingest</a>` });
  return out;
}
function poisonCardHtml(d) {
  const rows = d.files || [];
  const list = rows.map((r) => `<div class="tr t-poison">${mono(r.file)}${mono(r.collection || '')}<span class="why"><span title="${esc(r.error)}">${esc(r.said)}</span><span class="sub">${esc(r.why)}</span></span>${mono(String(r.tries))}<span class="muted">${r.last_tried ? ago(r.last_tried) : ''}</span><span class="row" style="justify-content: flex-end; gap: 6px"><button class="btn sm" ${on('click', ['retryFailed', r.id, r.file])}>Retry</button><button class="btn sm danger" ${on('click', ['dropFailed', r.id, r.file])}>Drop</button></span></div>`).join('');
  return `<div class="card"><div class="head"><h2>Would not read</h2><span class="faint">the poison queue, newest first · a file here is not in the index</span></div>
    ${pager('poison', { find: false, total: d.total || 0, from: d.offset || 0, count: rows.length })}
    <div class="table"><div class="tr head t-poison"><span>File</span><span>Collection</span><span>Why it stopped</span><span>Tries</span><span>Last tried</span><span></span></div>${list || '<div class="tr t-one"><span class="muted">Every file read. Nothing is waiting here.</span></div>'}</div>
    <div class="faint">Retry puts the message back on the ingest queue and starts from the Markdown when there is one. Drop deletes the file from raw/ and its Markdown and chunks; nothing of it stays. Both are written to the access log under your name.</div></div>`;
}
async function loadPoison() {
  await loadIngestState();
  const holder = $('ing-poison'); if (!holder) return;
  if (!canWrite()) { holder.innerHTML = ''; return; }
  try { holder.innerHTML = poisonCardHtml(await api(`/api/v1/ingest/failed?${pageQuery('poison')}`, { quiet: true })); } catch (e) { holder.innerHTML = ''; }
}
PAGERS.poison = () => loadPoison();
async function retryFailed(id, file) {
  try { await api(`/api/v1/ingest/failed/${encodeURIComponent(id)}/retry`, { method: 'POST' }); snack(`${file} is back on the queue`); } catch (e) { snack(e.message); }
  state.pages.poison = { q: '', page: 0 }; await loadPoison(); await loadPoisonCount();
}
function dropFailed(id, file) {
  sure(`Drop ${file}? The file, its Markdown and its chunks are deleted; nothing of it stays.`, 'Drop', async () => {
    try { await api(`/api/v1/ingest/failed/${encodeURIComponent(id)}/drop`, { method: 'POST' }); snack(`Dropped ${file}`); } catch (e) { snack(e.message); }
    state.pages.poison = { q: '', page: 0 }; await loadPoison(); await loadPoisonCount();
  });
}

/* ------------------------------------------------------- ingestion paused */
/* The breaker's state, from the app's own route: while the extraction app
   cannot be reached, ingestion waits and the page says since when and until
   when. Drawn at the top of Ingest and counted under Needs attention. */
function pausedLine(s) {
  return `<b>Ingestion is paused.</b> The ${esc(s.dependency || 'extraction app')} has not answered since ${esc(ago(s.since))}, ${countOf(s.failures || 0, 'read')} in a row. Files wait on the queue and nothing is lost; the next try is ${esc(s.until ? ago(s.until) : 'soon')}.`;
}
async function loadIngestState() {
  const holder = $('ing-state');
  if (!canWrite()) { state.ingestion = null; if (holder) holder.innerHTML = ''; return; }
  try { state.ingestion = (await api('/api/v1/ingest/state', { quiet: true })).ingestion || null; } catch (e) { state.ingestion = null; }
  if (holder) holder.innerHTML = state.ingestion && state.ingestion.paused ? `<div class="notice warn">${icon('alert')}<div>${pausedLine(state.ingestion)}</div></div>` : '';
}
