/* VectrixDB dashboard.
   Plain JavaScript, no build step. Hash routes: #/overview, #/collections,
   #/collections/<name>/<tab>, #/search, #/evaluate/chunking[/<technique>],
   #/evaluate/retrieval[/<setup>], #/ingest, #/audit, #/console. Every page reads the same server the page is
   served from. The dashboard is served at <root>/dashboard/, so API is what
   comes before that: the origin on a plain install, and the origin plus the
   path when a gateway serves the app under one. Taking the origin alone sent
   every call to the gateway's root, outside the route. */

const API = location.origin + location.pathname
  .replace(/index\.html$/, '')
  .replace(/\/dashboard\/?$/, '/')
  .replace(/\/+$/, '');
const state = {
  apiKey: null, authEnabled: false, readOnlyKey: false,
  info: null, collections: [], health: {}, models: null,
  page: 'overview', collection: null, tab: 'overview',
  searchMode: 'hybrid', theme: 'light', cy: null, docs: [],
  pointsOffset: 0, timings: [], pages: {}, docsTotal: 0,
  signinOn: false, guest: false, guestsOn: false, version: '', policies: null,
};
try { state.apiKey = localStorage.getItem('vectrixdb.apiKey') || null; } catch (e) {}
// Light is the default; dark is a choice this browser remembers.
try { state.theme = localStorage.getItem('vectrixdb.theme') || 'light'; } catch (e) {}

// A company's name, logo and line, when the server was deployed with them.
// VectrixDB's own name and version are under About, for admins.
// The brand arrives as data in the page, not as a script: the page runs no inline script.
const BRAND = (() => { try { const el = document.getElementById('vx-brand-data'); return el ? JSON.parse(el.textContent) : null; } catch (e) { return null; } })()
  || { name: 'VectrixDB', logo: null, logo_dark: null, custom: false };
const COPY = (BRAND && BRAND.copyright) || '© 2026 VectrixDB';
/* Behind a gateway that publishes each part of the server under a path of its
   own, the page is told the map, as data: each call goes to the gateway path
   its route was published under, then the prefix, then the route. With no map
   every route is under API, as it always was. */
const GATEWAY = (() => { try { const el = document.getElementById('vx-gateway-data'); return el ? JSON.parse(el.textContent) : null; } catch (e) { return null; } })();
function at(route) {
  if (!GATEWAY) return API + route;
  const path = String(route).split(/[?#]/)[0].replace(/^\/+|\/+$/g, '');
  let best = '';
  Object.keys(GATEWAY.paths || {}).forEach((name) => { if ((path === name || path.startsWith(name + '/')) && name.length > best.length) best = name; });
  return location.origin + (GATEWAY.root || '') + (best ? GATEWAY.paths[best] : '') + (GATEWAY.prefix || '') + route;
}
// The header a key goes in: api-key, unless the server was told another.
const KEY_HEADER = (GATEWAY && GATEWAY.key_header) || 'api-key';
function brandMark() {
  if (!BRAND.logo) return `<div class="mark">${icon('layers', 16)}</div>`;
  return `<div class="mark logo"><img class="logo-light" src="${esc(BRAND.logo)}" alt="">${BRAND.logo_dark ? `<img class="logo-dark" src="${esc(BRAND.logo_dark)}" alt="">` : ''}</div>`;
}
/* The brand the library's server writes into the page, written again from the
   data: a page some other service sends (a dashboard of your own, forwarding
   to the API) arrives plain, and is dressed here before anything shows. */
function applyBrand() {
  if (!BRAND.custom) return;
  document.title = BRAND.name;
  const name = $('brand-name'); if (name) { name.textContent = BRAND.name; name.parentElement.classList.toggle('wordmark', !!BRAND.wordmark); }
  const line = $('copyline'); if (line) line.textContent = COPY;
  const mark = $('brand-mark');
  if (BRAND.logo && mark && !mark.classList.contains('logo')) {
    mark.className = 'mark logo'; mark.removeAttribute('data-icon'); mark.innerHTML = brandMark().replace(/^<div class="mark logo">|<\/div>$/g, '');
    const tab = document.querySelector('link[rel="icon"]'); if (tab) { tab.href = BRAND.logo; tab.removeAttribute('type'); }
  }
}

/* ------------------------------------------------------------ helpers */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ------------------------------------------------------------ controls */
/* The page runs no inline script, so its policy refuses all of it. A control
   says what it does in data-on-<event>: a JSON list of calls, each a function's
   name and its arguments. One listener for each kind of event runs them.
   {"$": "this"} is the control, "value" and "checked" are its own, "event" is
   the event. data-stop keeps a click from reaching a row beneath, data-prevent
   stops a link from being followed, data-key names the one key a keydown is
   for. Only a function named in ACTIONS runs, so markup that got onto the page
   some other way cannot call whatever it likes. */
const THIS = { $: 'this' }; const VALUE = { $: 'value' }; const CHECKED = { $: 'checked' }; const EVENT = { $: 'event' };
function on(type, ...calls) { return `data-on-${type}="${esc(JSON.stringify(calls))}"`; }
/* -------------------------------------------------------------- pagers */
/* One bar for every list that can grow: a find box, where the page sits in
   the whole, and previous and next. Ten a page. Each list registers how it
   reloads in PAGERS under its id, and the bar's controls call that with the
   find and the page; the find and the page of each bar are kept in state, so
   a reload draws the same page again, and the find keeps its focus while
   the list under it is redrawn. */
const PAGE = 10;
const PAGERS = {};
function pageState(id) { return state.pages[id] || (state.pages[id] = { q: '', page: 0 }); }
function pageQuery(id) { const pg = pageState(id); return `limit=${PAGE}&offset=${pg.page * PAGE}${pg.q.trim() ? `&q=${encodeURIComponent(pg.q.trim())}` : ''}`; }
function pager(id, { placeholder, q, total, from, count, find = true }) {
  const last = total ? Math.max(0, Math.ceil(total / PAGE) - 1) : 0;
  const page = Math.floor((from || 0) / PAGE);
  const where = total ? `${fmtNum(from + 1)}–${fmtNum(from + count)} of ${fmtNum(total)}` : (q || '').trim() ? 'none match' : '0';
  return `<div class="pager" data-pager="${esc(id)}">${find ? `<input class="find" type="search" placeholder="${esc(placeholder || 'Find')}" aria-label="${esc(placeholder || 'Find')}" value="${esc(q || '')}" ${on('input', ['pageFind', id, VALUE])}>` : ''}<span class="grow"></span><span class="faint mono where">${where}</span><button class="btn icon" type="button" aria-label="Previous ${PAGE}" ${page === 0 ? 'disabled' : ''} ${on('click', ['pageTurn', id, -1])}>${icon('chevron-left', 16)}</button><button class="btn icon" type="button" aria-label="Next ${PAGE}" ${page >= last ? 'disabled' : ''} ${on('click', ['pageTurn', id, 1])}>${icon('chevron-right', 16)}</button></div>`;
}
function pageRefocus(id) { const el = document.querySelector(`[data-pager="${id}"] .find`); if (el && document.activeElement !== el) { el.focus(); try { el.setSelectionRange(el.value.length, el.value.length); } catch (e) {} } }
function pageFind(id, value) {
  const pg = pageState(id); pg.q = value; pg.page = 0;
  clearTimeout(pg.timer);
  pg.timer = setTimeout(() => { if (PAGERS[id]) Promise.resolve(PAGERS[id](pg.q, pg.page)).then(() => pageRefocus(id)); }, 180);
}
function pageTurn(id, delta) { const pg = pageState(id); pg.page = Math.max(0, pg.page + delta); if (PAGERS[id]) PAGERS[id](pg.q, pg.page); }

const ACTIONS = new Set(['pressSso', 'checkSso', 'openEmailWay', 'addAdmin', 'evFind', 'pageFind', 'pageTurn', 'addPasskey', 'addPerson', 'addWhoRow', 'checkSomeone', 'dropWhoRow', 'saveWho', 'whoInput', 'whoPasteAdd', 'whoPasteInput', 'whoPasteToggle', 'changePassword', 'ckOlderRuns', 'ckOpenRun', 'clearKey', 'closeDialog', 'closeGate', 'confirmAuthenticator', 'confirmDelete', 'consoleSearch', 'copyCode', 'copyFrom', 'copySearchSnippet', 'copyText', 'createCollection', 'deleteDoc', 'deletePoint', 'dropFile', 'endOtherSessions', 'endSession', 'enrolPasskey', 'enrolWithAppInstead', 'evFilter', 'evOlderRuns', 'evOpenRun', 'evPage', 'evToggleRuns', 'evTry', 'forgotPassword', 'gateCode', 'gateEmail', 'gateEnrolBegin', 'gateEnrolConfirm', 'gateResetPassword', 'go', 'ingTarget', 'leaveSso', 'lookupProvenance', 'makeFirstPasskey', 'makeKey', 'newRecoveryCodes', 'openDialog', 'openDocument', 'openKeyDialog', 'openPerson', 'pickWho', 'pickWay', 'pointsPage', 'presetConsole', 'rebuild', 'reloadAt', 'removePasskey', 'removePerson', 'renderCollections', 'replaceAuthenticator', 'resetPerson', 'revokeKey', 'runConsole', 'runIngest', 'runSearch', 'saveKey', 'savePassword', 'searchIn', 'setColFilter', 'setDocumentRead', 'setMode', 'setRole', 'setTab', 'setTheme', 'showCodeWay', 'showGate', 'signInWithPasskey', 'signOut', 'startSso', 'stepUpCode', 'stepUpDone', 'stepUpPasskey', 'stepUpSso', 'switchTab', 'toggleChunk', 'togglePw', 'trySearch', 'useRecovery', 'breakGlassSignIn', 'leaveBreakGlass', 'openAbout', 'gateDeveloper', 'developerSignIn', 'leaveDeveloper', 'removeAuthenticator', 'stepUpPassword']);
function runControl(el, type, event) {
  let calls;
  try { calls = JSON.parse(el.getAttribute(`data-on-${type}`)); } catch (e) { return; }
  if (!Array.isArray(calls)) return;
  const value = (a) => (a && typeof a === 'object' && !Array.isArray(a) && typeof a.$ === 'string' ? { this: el, value: el.value, checked: el.checked, event }[a.$] : a);
  for (const call of calls) {
    if (!Array.isArray(call) || !ACTIONS.has(call[0]) || typeof window[call[0]] !== 'function') continue;
    window[call[0]].apply(el, call.slice(1).map(value));
  }
}
['click', 'change', 'input', 'keydown', 'submit'].forEach((type) => document.addEventListener(type, (event) => {
  const el = event.target instanceof Element ? event.target.closest(`[data-on-${type}]`) : null;
  if (!el) return;
  if (type === 'keydown' && el.dataset.key && event.key !== el.dataset.key) return;
  if (el.hasAttribute('data-stop')) event.stopPropagation();
  if (type === 'submit' || el.hasAttribute('data-prevent')) event.preventDefault();
  runControl(el, type, event);
}));

/* The few controls that do more than call one function. */
function ingTarget(value) { $('ing-coll-wrap').style.display = value === 'documents' ? 'none' : ''; }
function clearKey() { $('key-input').value = ''; saveKey(); }
function copyFrom(id) { copyText($(id).textContent); }
function copySearchSnippet() { copyText(searchSnippet()); }
function dropFile(i) { ingestFiles.splice(i, 1); renderFiles(); }
function reloadAt(hash) { location.hash = hash; location.reload(); }
function enrolWithAppInstead() { state.enrolChoice = 'authenticator'; enrolWithApp(); }
function pointsPage(delta) { pageTurn('points', Math.sign(delta)); }
function showCodeWay(button) { button.hidden = true; $('gate-code-way').hidden = false; $('gate-email').focus(); }
function addAdmin() { go('#/access'); setTimeout(openPerson, 80); }
function setDocumentRead(email, role, checked) { savePerson(email, role, checked ? ['document.read'] : []); }
function consoleSearch(name) { go('#/console'); setTimeout(() => presetConsole('search', name), 50); }
const mono = (s, cls = '') => `<span class="mono ${cls}">${esc(s)}</span>`;
const pill = (text, kind = '') => `<span class="pill ${kind}">${esc(text)}</span>`;
const tag = (t) => `<span class="tag">${esc(t)}</span>`;
const fmtBytes = (n) => { if (!n && n !== 0) return '–'; const u = ['B', 'KB', 'MB', 'GB']; let i = 0; let v = n; while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; } return `${v.toFixed(i ? 1 : 0)} ${u[i]}`; };
const fmtNum = (n) => (n === null || n === undefined) ? '–' : Number(n).toLocaleString();
// A count and its noun: "1 chunk", "2 chunks". Every count shown with a noun goes through here.
const countOf = (n, one, many = `${one}s`) => `${fmtNum(n)} ${Number(n) === 1 ? one : many}`;
// "5m ago", "3h ago", "2d ago": the number and its unit together, no gap.
// Past a week it is a date, which reads better than "23d ago".
const ago = (iso) => { if (!iso) return '–'; const d = (Date.now() - new Date(iso).getTime()) / 1000; if (d < 60) return 'just now'; if (d < 3600) return `${Math.round(d / 60)}m ago`; if (d < 86400) return `${Math.round(d / 3600)}h ago`; if (d < 86400 * 7) return `${Math.round(d / 86400)}d ago`; return new Date(iso).toLocaleDateString([], { day: 'numeric', month: 'short', year: new Date(iso).getFullYear() === new Date().getFullYear() ? undefined : 'numeric' }); };
const agoTs = (seconds) => seconds ? ago(new Date(seconds * 1000).toISOString()) : '–';
const dayOf = (seconds) => seconds ? new Date(seconds * 1000).toLocaleDateString([], { day: 'numeric', month: 'short' }) : '–';
// An identifier is for copying, not for reading: the prefix goes, eight
// characters stay, the whole of it is the tooltip and a click copies it.
const idChip = (id) => { if (!id) return '<span class="faint">–</span>'; const full = String(id); const bare = full.replace(/^[a-z]+_/, ''); return `<button type="button" class="id" title="${esc(full)}. Click to copy." ${on('click', ['copyText', full])} data-stop>${esc(bare.slice(0, 8))}${icon('copy', 12)}</button>`; };
const short = (s, n = 8) => { s = String(s ?? ''); return s.length > n + 2 ? s.slice(0, n) + '…' : s; };

function headers(json = true) {
  const h = {};
  if (json) h['Content-Type'] = 'application/json';
  if (state.apiKey) h[KEY_HEADER] = state.apiKey;
  // A request that changes something has to carry the session's token as
  // well as its cookie. The page can read the token; a page on another site cannot.
  // Over https the cookie is __Host-vx_csrf, bound to this host alone; on a laptop it is plain.
  const token = cookie('__Host-vx_csrf') || cookie('vx_csrf'); if (token) h['X-CSRF-Token'] = token;
  return h;
}
function cookie(name) { const m = document.cookie.split('; ').find((c) => c.startsWith(name + '=')); return m ? decodeURIComponent(m.slice(name.length + 1)) : null; }

async function api(path, opts = {}) {
  const res = await fetch(at(path), { ...opts, headers: { ...headers(!!opts.body), ...(opts.headers || {}) } });
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  if (res.status === 401 && body && body.data && body.data.signin && !opts.quiet) showGate();
  // A change that matters asks again that it is really you, then goes ahead.
  if (res.status === 403 && body && body.data && body.data.step_up && !opts.stepped) {
    if (await stepUp(body.data.ways || [])) return api(path, { ...opts, stepped: true });
  }
  if (!res.ok) {
    let detail = (body && (body.detail || body.message)) || `${res.status} ${res.statusText}`;
    if (typeof detail === 'object') detail = detail.msg || detail.message || JSON.stringify(detail);
    const err = new Error(detail); err.status = res.status; err.body = body; throw err;
  }
  return body && body.data !== undefined && body.ok !== undefined ? body.data : body;
}

async function apiText(path) {
  const res = await fetch(at(path), { headers: headers(false) });
  if (!res.ok) { let detail = `${res.status} ${res.statusText}`; try { const b = await res.json(); detail = b.detail || b.message || detail; } catch (e) { /* not JSON */ } const err = new Error(detail); err.status = res.status; throw err; }
  return res.text();
}

/* ------------------------------------------------------------ table labels */
/* On a phone a table row is a stack of values, and a value without its column
   heading is a riddle. Every cell is given its heading as data-label, read
   from the head row of its table, whenever the page renders something. A
   cell whose text is cut off gets the whole of it as a tooltip. */
function labelTables(root) {
  (root || document).querySelectorAll('.table').forEach((table) => {
    const head = table.querySelector(':scope > .tr.head'); if (!head) return;
    const labels = [...head.children].map((c) => c.textContent.trim());
    table.querySelectorAll(':scope > .tr:not(.head)').forEach((row) => {
      [...row.children].forEach((cell, i) => {
        if (labels[i]) cell.setAttribute('data-label', labels[i]); else cell.removeAttribute('data-label');
        if (!cell.title && cell.children.length === 0 && cell.scrollWidth > cell.clientWidth + 1) cell.title = cell.textContent.trim();
      });
    });
  });
}
let labelTimer = null;
function watchTables() {
  const main = document.querySelector('main'); if (!main) return;
  labelTables(main);
  new MutationObserver(() => { clearTimeout(labelTimer); labelTimer = setTimeout(() => labelTables(main), 40); }).observe(main, { childList: true, subtree: true });
}

let snackTimer = null;
function snack(msg) {
  const el = $('snackbar'); el.textContent = msg; el.classList.add('show');
  clearTimeout(snackTimer); snackTimer = setTimeout(() => el.classList.remove('show'), 3200);
}

function icon(name, size = 16) {
  // Lucide's paths (lucide.dev, ISC licence), inlined because this page has to
  // work with no network. One family, one grid, one stroke, so a row of them
  // reads as a set.
  const paths = {
    home: '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    folder: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
    search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
    shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
    terminal: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="m7 11 2-2-2-2"/><path d="M11 13h4"/>',
    book: '<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>',
    settings: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    layers: '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z"/><path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12"/><path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17"/>',
    lock: '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    unlock: '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9"/>',
    menu: '<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/>',
    'panel-close': '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/><path d="m16 15-3-3 3-3"/>',
    'panel-open': '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/><path d="m14 9 3 3-3 3"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    play: '<path d="m6 3 14 9-14 9z"/>',
    copy: '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    key: '<path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4"/><path d="m21 2-9.6 9.6"/><circle cx="7.5" cy="15.5" r="5.5"/>',
    cpu: '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/>',
    scan: '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/>',
    git: '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
    'log-in': '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="m10 17 5-5-5-5"/><path d="M15 12H3"/>',
    'log-out': '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    'key-round': '<path d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z"/><circle cx="16.5" cy="7.5" r=".5" fill="currentColor"/>',
    smartphone: '<rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/>',
    'life-buoy': '<circle cx="12" cy="12" r="10"/><path d="m4.93 4.93 4.24 4.24"/><path d="m14.83 9.17 4.24-4.24"/><path d="m14.83 14.83 4.24 4.24"/><path d="m9.17 14.83-4.24 4.24"/><circle cx="12" cy="12" r="4"/>',
    'user-x': '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="m17 8 5 5"/><path d="m22 8-5 5"/>',
    'user-plus': '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6"/><path d="M22 11h-6"/>',
    'shield-alert': '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
    'chevron-down': '<path d="m6 9 6 6 6-6"/>',
    mail: '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.991 5.727a2 2 0 0 1-2.009 0L2 7"/>',
    eye: '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
    plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
    globe: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    monitor: '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/>',
    code: '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
    'arrow-right': '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    'arrow-left': '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    'chevron-left': '<path d="m15 18-6-6 6-6"/>',
    'chevron-right': '<path d="m9 18 6-6-6-6"/>',
    gauge: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
    cloud: '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
    scissors: '<circle cx="6" cy="6" r="3"/><path d="M8.12 8.12 12 12"/><path d="M20 4 8.12 15.88"/><circle cx="6" cy="18" r="3"/><path d="M14.8 14.8 20 20"/>',
  };
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || ''}</svg>`;
}

// What a collection can be searched by, and what it is built with, as the
// server reports them. A tier tag like "dense" says how it was opened once;
// these say what is true of it now, so they are what a card shows.
const TIER_TAGS = ['dense', 'sparse', 'hybrid', 'ultimate', 'graph'];
const SEARCH_WAYS = [['dense', 'Dense', 'Search by meaning'], ['keyword', 'Keyword', 'Search by the words themselves'], ['hybrid', 'Hybrid', 'Both, fused']];
function capsOf(c, h) { return (h && h.capabilities) || { dense: true, keyword: !!c.has_text_index, hybrid: !!c.has_text_index }; }
function svc(iconName, label, title) { return `<span class="tag svc" title="${esc(title)}">${icon(iconName, 12)}<span>${esc(label)}</span></span>`; }
function builtWith(c, h) {
  const s = (h && h.services) || {};
  const out = [];
  out.push(svc('folder', s.stored_in || state.info?.storage_backend || 'SQLite', 'Where the vectors are stored'));
  (s.embedded_by && s.embedded_by.length ? s.embedded_by : (h && h.embedding_model ? [h.embedding_model] : [])).forEach((m) => out.push(svc('cpu', m, 'The model that embedded the text')));
  (s.extracted_by || []).forEach((x) => out.push(svc('scan', x, 'What read the files')));
  return out.join('');
}
function searchWays(c, h) { const caps = capsOf(c, h); return SEARCH_WAYS.filter(([k]) => caps[k]).map(([, label, title]) => `<span class="tag way" title="${esc(title)}">${label}</span>`).join(''); }
function ownTags(c) { return (c.tags || []).filter((t) => !TIER_TAGS.includes(String(t).toLowerCase())); }

function modeOf(c) {
  const tags = (c.tags || []).map((t) => String(t).toLowerCase());
  if (tags.includes('graph')) return 'graph';
  if (tags.includes('ultimate')) return 'ultimate';
  if (tags.includes('hybrid') || c.has_text_index) return 'hybrid';
  return 'dense';
}

/* A collection with a policy: an entitlement policy bound to it, or, on a server that gates
   who may search, a collection somebody has been named for. The Overview counts the same. */
function hasPolicy(name) {
  const pol = state.policies; const v = pol && pol.gated && pol.collections ? pol.collections[name] : null;
  return stateOf(name).text === 'Policied' || !!(v && v.method);
}
function stateOf(name) {
  const h = state.health[name] || {};
  const c = state.collections.find((x) => x.name === name) || {};
  if (h.policied || c.entitlement_policy) return { text: 'Policied', kind: 'warn', icon: 'lock' };
  if (h.state === 'rebuild') return { text: 'Rebuild advised', kind: 'warn', icon: 'alert' };
  if (['e5-small-v2', 'dense_en'].includes(h.embedding_model) && state.models && !modelPresent('dense_en')) return { text: 'Model missing', kind: 'bad', icon: 'x' };
  if ((h.count ?? c.count) === 0) return { text: 'Empty', kind: '', icon: '' };
  return { text: 'Healthy', kind: 'ok', icon: 'check' };
}
function modelPresent(type) { const m = (state.models?.models || []).find((x) => x.type === type); return !!(m && m.present); }
// A guest may look at the collections shared with everyone and how the setups scored, and nothing else: no search.
const GUEST_ACTIONS = ['meta.read', 'evaluation.read'];
function can(action) { if (state.guest) return GUEST_ACTIONS.includes(action); return state.me ? state.me.actions.includes(action) : true; }
function canWrite() { if (state.guest) return false; return state.me ? can('content.write') : (!state.authEnabled || !!state.apiKey); }

/* --------------------------------------------------------------- theme */
function applyTheme() {
  document.documentElement.setAttribute('data-theme', state.theme);
  document.querySelectorAll('[data-theme-chip]').forEach((b) => b.classList.toggle('on', b.dataset.themeChip === state.theme));
}
function setTheme(t) { state.theme = t; try { localStorage.setItem('vectrixdb.theme', t); } catch (e) {} applyTheme(); }

/* --------------------------------------------------------------- auth */
function renderAuth() {
  const el = $('pill-auth');
  if (!state.authEnabled) { el.className = 'pill info'; el.innerHTML = `${icon('lock', 12)} Open, no key set`; }
  else if (state.apiKey) { el.className = 'pill ok'; el.innerHTML = `${icon('check', 12)} Authenticated`; }
  else { el.className = 'pill warn'; el.innerHTML = `${icon('lock', 12)} Read only`; }
  $('btn-key').innerHTML = icon('lock', 16) + esc(state.authEnabled ? (state.apiKey ? 'Change the API key' : 'Enter the API key') : 'API key');
}
async function loadAuth() {
  try {
    const d = await api('/auth/status');
    state.authEnabled = !!d.auth_enabled; state.readOnlyKey = !!d.read_only_key_enabled;
  } catch (e) { state.authEnabled = false; }
  renderAuth();
}
function openKeyDialog() {
  $('key-input').value = state.apiKey || '';
  $('key-note').textContent = state.authEnabled
    ? 'This server has an API key. Writes and the policy and audit routes need it. It is kept in this browser only.'
    : 'This server has no API key configured, so nothing here needs one. Start it with --api-key to require one.';
  openDialog('dlg-key');
}
function saveKey() {
  const v = $('key-input').value.trim();
  state.apiKey = v || null;
  try { v ? localStorage.setItem('vectrixdb.apiKey', v) : localStorage.removeItem('vectrixdb.apiKey'); } catch (e) {}
  closeDialog('dlg-key'); renderAuth(); refresh();
  snack(v ? 'Key saved in this browser' : 'Key cleared');
}
function openDialog(id) { $(id).classList.add('open'); const f = $(id).querySelector('input,textarea,select'); if (f) setTimeout(() => f.focus(), 30); }
function closeDialog(id) { $(id).classList.remove('open'); }

/* ------------------------------------------------------------- routing */
const PAGES = ['overview', 'collections', 'collection', 'search', 'evaluate', 'ingest', 'audit', 'access', 'console'];
const TITLES = { overview: 'Overview', collections: 'Collections', collection: 'Collection', search: 'Search', evaluate: 'Evaluate', ingest: 'Ingest', audit: 'Audit', access: 'Access', console: 'Console' };

// The action a page is for. A page somebody may not use is not in their menu, and its address goes to the Overview.
const NEEDS = { search: 'search', evaluate: 'evaluation.read', ingest: 'content.write', audit: 'audit.read', access: 'access.read', console: 'content.read' };

function route() {
  const parts = (location.hash.replace(/^#\/?/, '') || 'overview').split('/').map(decodeURIComponent);
  let page = parts[0];
  if (page === 'collections' && parts[1]) { page = parts[2] === 'setup' ? 'setup' : 'collection'; state.collection = parts[1]; state.tab = parts[2] || 'overview'; }
  // Evaluate has two tabs, Chunking first. An address from before the tabs, #/evaluate/<setup>, is that setup under Retrieval.
  if (page === 'evaluate' && parts[1] && !['chunking', 'retrieval'].includes(parts[1])) { location.replace(`#/evaluate/retrieval/${encodeURIComponent(parts[1])}`); return; }
  state.evalTab = page === 'evaluate' ? (parts[1] || 'chunking') : null;
  state.evalSetup = state.evalTab === 'retrieval' ? (parts[2] || null) : null;
  state.chunkTech = state.evalTab === 'chunking' ? (parts[2] || null) : null;
  if (!PAGES.includes(page) || (NEEDS[page] && !can(NEEDS[page]))) page = 'overview';
  state.page = page;
  PAGES.forEach((p) => $(`page-${p}`).classList.toggle('active', p === page));
  document.querySelectorAll('.nav a').forEach((a) => a.classList.toggle('active', a.dataset.page === (page === 'collection' || page === 'setup' ? 'collections' : page)));
  $('top-title').textContent = page === 'collection' || page === 'setup' ? state.collection : TITLES[page];
  $('side').classList.remove('open');
  if (page === 'evaluate') {
    ['chunking', 'retrieval'].forEach((t) => { const b = $(`etab-${t}`); b.classList.toggle('on', state.evalTab === t); b.setAttribute('aria-selected', String(state.evalTab === t)); });
    $('top-back').setAttribute('href', `#/evaluate/${state.evalTab}`);
  }
  // On a phone, one setup's or one technique's page has a way back in the top bar where the menu was.
  document.body.classList.toggle('ev-setup-open', page === 'evaluate' && !!(state.evalSetup || state.chunkTech));
  loadPage(page);
}
function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
function showPage(p) { go(`#/${p}`); }

async function loadPage(page) {
  try {
    if (page === 'overview') await loadOverview();
    else if (page === 'collections') await loadCollections();
    else if (page === 'collection') await loadCollection();
    else if (page === 'setup') await loadSetup();
    else if (page === 'search') await loadSearchPage();
    else if (page === 'evaluate') await (state.evalTab === 'chunking' ? loadChunking() : loadEvaluate());
    else if (page === 'ingest') await loadIngest();
    else if (page === 'audit') await loadAudit();
    else if (page === 'access') await loadAccess();
  } catch (e) { snack(e.message); }
}
function refresh() { loadPage(state.page); }

/* ------------------------------------------------------- shared loads */
async function loadInfo() {
  state.info = await api('/api/v1/info');
  $('pill-backend').textContent = (state.info.storage_backend || 'sqlite').toUpperCase();
}
async function loadCollectionList() {
  const d = await api('/api/v1/collections');
  state.collections = d.collections || d.data?.collections || (Array.isArray(d) ? d : []);
  state.collectionsRead = true;
  await Promise.all(state.collections.map(async (c) => {
    try { state.health[c.name] = await api(`/api/v1/collections/${encodeURIComponent(c.name)}/health`); } catch (e) { state.health[c.name] = {}; }
  }));
  fillCollectionSelects();
}
async function loadModels() { if (!state.models) { try { state.models = await api('/api/v1/models'); } catch (e) { state.models = { models: [] }; } } }
function fillCollectionSelects() {
  document.querySelectorAll('select[data-collections]').forEach((sel) => {
    const cur = sel.value;
    sel.innerHTML = state.collections.length ? state.collections.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('') : '<option value="">No collections</option>';
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
    else if (state.collection && [...sel.options].some((o) => o.value === state.collection)) sel.value = state.collection;
  });
}

/* ------------------------------------------------------------ overview */
async function loadPolicies() {
  if (!state.signinOn) { state.policies = null; return; }
  try { state.policies = await api('/api/v1/policies'); } catch (e) { state.policies = null; }
}
/* One tag: how many the policy admits. Names on hover, for people signed in; a guest gets the count alone.
   A policy of groups counts both: the groups, and the people on its list. */
function whoTag(v) {
  if (!v || !v.method) return '';
  const count = v.method === 'token' ? [countOf(v.groups, 'group'), countOf(v.people || 0, 'person', 'people')].join(', ') : [v.people ? countOf(v.people, 'person', 'people') : '', v.domains ? countOf(v.domains, 'domain') : ''].filter(Boolean).join(', ');
  return `<span class="pill" title="${esc(v.policy ? describePolicy(v.policy) : '')}">${icon('lock', 12)}${esc(count)}</span>`;
}
function whoSearches(name) {
  const pol = state.policies; const v = pol && pol.collections[name];
  if (!v || !pol.gated) return '';
  if (!v.method) return `<span class="pill warn" title="No policy on the record yet: nobody can search it until an admin sets one">${icon('alert', 12)}Unavailable to anyone yet</span>`;
  return whoTag(v);
}
function setOverviewFor(guest) {
  $('ov-guest').hidden = !guest; $('ov-attn-card').hidden = guest; $('ov-start').hidden = guest;
  $('ov-col-actions').hidden = guest; $('ov-new').hidden = !can('collection.create');
  // A guest has no side column, so the collections take the width.
  $('ov-side').hidden = guest; $('ov-body').classList.toggle('lay-one', guest);
  // A way in that the role cannot take is not offered: a viewer does not search.
  $('ov-search').hidden = !can('search');
  $('ov-alerts').hidden = true;
}
/* Searches a day and search time from the access log, and what was written
   across every collection: for everyone, guests included. The route keeps
   the sign-in counts back from whoever may not read the log, and the searches
   a policy refused come with them, so a guest's chart has no red line. No row
   at all, rather than empty charts, when there is nothing to count from: a
   server with no collections yet, or no log and nothing written. */
async function loadTrends() {
  const holder = $('ov-trends'); if (!holder) return;
  if (!state.collections.length) { holder.hidden = true; return; }
  try {
    const [d, g] = await Promise.all([
      api('/api/v1/access/daily?days=14', { quiet: true }).catch(() => null),
      api('/api/v1/growth?days=14', { quiet: true }).catch(() => null),
    ]);
    const counts = d && d.available ? d : null;
    if (!counts && !g) { holder.hidden = true; return; }
    if (!$('ov-trends')) return;
    holder.hidden = false; trOverview(holder, counts, g, { ingestLink: !state.guest && can('content.write') ? '#/ingest' : null });
  } catch (e) { holder.hidden = true; }
}

async function loadOverview() {
  if (state.guest) return loadGuestOverview();
  setOverviewFor(false);
  await Promise.all([loadInfo(), loadCollectionList(), loadModels(), loadPolicies(), typeof loadPoisonCount === 'function' ? loadPoisonCount() : null]);
  const info = state.info;
  const attention = attentionItems();
  const policied = state.collections.filter((c) => stateOf(c.name).text === 'Policied').length;
  const pol = state.policies;
  const unset = pol && pol.gated ? Object.values(pol.collections).filter((v) => !v.method).length : null;
  $('ov-metrics').innerHTML = [
    metric('Collections', fmtNum(info.collections_count), unset === null ? (policied ? `${policied} policied` : '') : unset ? `${countOf(unset, 'collection')} unavailable to anyone yet` : 'each with a policy'),
    // Documents are counted where the server keeps a document index; where it does not, the line says what the number spans.
    metric('Vectors', fmtNum(info.total_vectors), info.documents_count ? countOf(info.documents_count, 'document') : `across ${countOf(info.collections_count, 'collection')}`),
    info.shared_store ? metric('Kept in', 'Shared store', 'read by every instance') : metric('On disk', fmtBytes(info.total_size_bytes), info.storage_backend || 'sqlite'),
    metric('Search p50', p50(), state.timings.length ? `${state.timings.length} queries this session` : 'no queries yet'),
  ].join('');
  loadTrends();
  // What needs attention is a list, with its count on the card that holds it.
  const notices = attention.map((a) => `<div class="notice ${a.kind}">${icon(a.icon)}<div>${a.html}</div></div>`).join('');
  $('ov-attn-count').innerHTML = attention.length ? pill(String(attention.length), 'bad') : '';
  if (!state.collections.length) {
    $('ov-alerts').innerHTML = notices; $('ov-alerts').hidden = !attention.length;
    $('ov-body').style.display = 'none'; $('ov-empty').style.display = '';
    $('ov-empty-note').textContent = canWrite()
      ? 'Nothing is indexed yet. Create a collection, then add a document from Ingest.'
      : 'This server has an API key. Enter it to create a collection.';
    return;
  }
  $('ov-body').style.display = ''; $('ov-empty').style.display = 'none'; $('ov-alerts').hidden = true;
  $('ov-attention').innerHTML = notices || `<div class="notice">${icon('check')}<div>Nothing needs a human right now.</div></div>`;
  renderOvCollections();
}
/* The collections on the Overview, newest write first, ten a page. With
   records on, who may search a collection is the first thing to know about
   it. A guest's rows open nothing: a collection's page is for people signed in. */
function renderOvCollections() {
  const holder = $('ov-collections'); if (!holder) return;
  const pg = pageState('ovcols'); const pol = state.policies; const gated = !!(pol && pol.gated);
  const when = (c) => state.health[c.name]?.updated_at || c.updated_at || c.created_at || '';
  const sorted = [...state.collections].sort((a, b) => String(when(b)).localeCompare(String(when(a))));
  const pages = Math.max(1, Math.ceil(sorted.length / PAGE)); pg.page = Math.min(pg.page, pages - 1);
  const from = pg.page * PAGE, shown = sorted.slice(from, from + PAGE);
  const open = (c) => state.guest ? '' : on('click', ['go', '#/collections/' + encodeURIComponent(c.name)]);
  const cls = state.guest ? '' : 'click';
  const rows = gated
    ? shown.map((c) => { const h = state.health[c.name] || {}; return `<div class="tr ${cls} t-seen" ${open(c)}>${mono(c.name)}<span>${whoSearches(c.name)}</span>${mono(h.policied ? 'withheld' : fmtNum(h.count ?? c.count))}<span class="muted">${ago(when(c))}</span></div>`; }).join('')
    : shown.map((c) => { const s = stateOf(c.name); const h = state.health[c.name] || {}; return `<div class="tr ${cls} t-collections" ${open(c)}>${mono(c.name)}<span>${pill(s.text, s.kind)}</span><span>${modeOf(c)}</span>${mono(h.policied ? 'withheld' : fmtNum(h.count ?? c.count))}<span class="muted">${ago(when(c))}</span></div>`; }).join('');
  const head = gated ? `<div class="tr head t-seen"><span>Name</span><span>Who can search it</span><span>Chunks</span><span>Updated</span></div>` : `<div class="tr head t-collections"><span>Name</span><span>State</span><span>Mode</span><span>Chunks</span><span>Last write</span></div>`;
  holder.innerHTML = `${pager('ovcols', { find: false, total: sorted.length, from, count: shown.length })}<div class="table">${head}${rows || '<div class="tr t-one"><span class="muted">No collections yet.</span></div>'}</div>`;
}
PAGERS.ovcols = () => renderOvCollections();
/* A guest's Overview is the same page without what is about people: three
   tiles, the charts, and the collections full width. */
async function loadGuestOverview() {
  setOverviewFor(true);
  await Promise.all([loadCollectionList(), loadModels(), loadPolicies()]);
  const cols = state.collections;
  const chunks = cols.reduce((n, c) => n + Number((state.health[c.name] || {}).count ?? c.count ?? 0), 0);
  const newest = cols.map((c) => ({ name: c.name, at: (state.health[c.name] || {}).updated_at || c.updated_at || c.created_at })).filter((x) => x.at).sort((a, b) => String(b.at).localeCompare(String(a.at)))[0];
  const pol = state.policies;
  const unset = pol && pol.gated ? Object.values(pol.collections).filter((v) => !v.method).length : null;
  $('ov-guest-text').textContent = cols.length ? 'Sign in to search.' : 'This server has no collections yet. Sign in to make one.';
  $('ov-metrics').innerHTML = [
    metric('Collections', String(cols.length), unset === null ? '' : unset ? `${countOf(unset, 'collection')} unavailable to anyone yet` : 'each with a policy'),
    metric('Chunks', fmtNum(chunks), `across ${countOf(cols.length, 'collection')}`),
    metric('Last update', newest ? ago(newest.at) : null, newest ? newest.name : ''),
  ].join('');
  loadTrends();
  $('ov-body').style.display = ''; $('ov-empty').style.display = 'none';
  renderOvCollections();
}
function searchIn(name) { go('#/search'); setTimeout(() => { $('s-collection').value = name; availableModes(); }, 50); }
// A tile with nothing to show yet says so in words, not with a dash.
function metric(label, value, sub = '', kind = '') { const none = value === null || value === undefined || value === ''; return `<div class="metric ${none ? '' : kind}"><div class="l">${esc(label)}</div><div class="v${none ? ' none' : String(value).length > 9 ? ' long' : ''}">${none ? 'None yet' : esc(value)}</div>${sub ? `<div class="s">${esc(sub)}</div>` : ''}</div>`; }
function p50() { if (!state.timings.length) return null; const s = [...state.timings].sort((a, b) => a - b); return `${Math.round(s[Math.floor(s.length / 2)])} ms`; }
function attentionItems() {
  const out = [];
  if (typeof poisonAttention === 'function') out.push(...poisonAttention());
  // One admin is one lost phone away from a locked door. A second can reset the first from here.
  if (state.me && state.me.role === 'admin' && typeof state.me.admins === 'number' && state.me.admins <= 1) {
    out.push({ kind: 'warn', icon: 'shield-alert', admin: true, html: `<b>You're the only admin.</b> If your phone and your recovery codes are both lost, getting back in means running a command on the server. A second admin could reset you from here instead.<div class="row" style="margin-top: 8px"><button class="btn sm" data-on-click="[[&quot;addAdmin&quot;]]">${icon('user-plus', 14)} Add an admin</button></div>` });
  }
  for (const c of state.collections) {
    const h = state.health[c.name] || {};
    if (h.state === 'rebuild') out.push({ kind: 'warn', icon: 'alert', html: `<b>${esc(c.name)}</b> is ${Math.round(h.tombstone_ratio * 100)}% deleted vectors, so every search does work for documents that are gone. <a href="#/collections/${encodeURIComponent(c.name)}/builds">Rebuild</a>` });
    if (['e5-small-v2', 'dense_en'].includes(h.embedding_model) && state.models && !modelPresent('dense_en')) out.push({ kind: 'bad', icon: 'x', html: `<b>${esc(c.name)}</b> was written with e5-small-v2, which is not on this machine. <a href="#/settings">Download it</a> or reembed to the current model.` });
  }
  return out;
}

/* --------------------------------------------------------- collections */
let collectionFilter = 'all';
async function loadCollections() {
  await Promise.all([loadCollectionList(), loadModels(), loadPolicies()]);
  renderCollections();
}
/* A grid that fills as many columns as fit leaves one card alone on the last
   row whenever the count is one more than a multiple: four cards in three
   columns. The column count is picked here instead: the most that fit the
   width without that, which for four cards is two rows of two. */
function fitCards() {
  const grid = $('col-grid'); if (!grid) return;
  const n = grid.querySelectorAll(':scope > .ccard').length;
  if (!n) { grid.removeAttribute('data-cols'); return; }
  const most = Math.max(1, Math.min(n, Math.floor((grid.clientWidth + 12) / (280 + 12))));
  let cols = most;
  while (cols > 1 && n > cols && n % cols === 1) cols -= 1;
  grid.style.setProperty('--cols', cols); grid.setAttribute('data-cols', cols);
}
window.addEventListener('resize', () => { clearTimeout(state.fitTimer); state.fitTimer = setTimeout(fitCards, 120); });

function renderCollections() {
  const q = ($('col-filter').value || '').toLowerCase();
  const pg = pageState('cols');
  if (pg.seen !== q + '|' + collectionFilter) { pg.page = 0; pg.seen = q + '|' + collectionFilter; }
  const list = state.collections.filter((c) => {
    const s = stateOf(c.name).text;
    if (collectionFilter === 'policied' && !hasPolicy(c.name)) return false;
    if (collectionFilter === 'attention' && !['Rebuild advised', 'Model missing'].includes(s)) return false;
    if (['hybrid', 'graph', 'dense'].includes(collectionFilter) && modeOf(c) !== collectionFilter) return false;
    if (q && !(c.name.toLowerCase().includes(q) || (c.tags || []).join(' ').toLowerCase().includes(q) || (c.description || '').toLowerCase().includes(q))) return false;
    return true;
  });
  const counts = { all: state.collections.length, policied: state.collections.filter((c) => hasPolicy(c.name)).length, attention: state.collections.filter((c) => ['Rebuild advised', 'Model missing'].includes(stateOf(c.name).text)).length };
  document.querySelectorAll('[data-col-filter]').forEach((b) => { b.classList.toggle('on', b.dataset.colFilter === collectionFilter); const n = counts[b.dataset.colFilter]; if (n !== undefined) b.textContent = `${b.dataset.label} ${n}`; });
  $('col-new').hidden = !can('collection.create');
  const grid = $('col-grid'); grid.className = 'card list';
  if (!state.collections.length && state.guest) { grid.innerHTML = `<div class="empty"><div class="glyph">${icon('globe', 28)}</div><h2>No collections yet</h2><div>Sign in to make one.</div></div>`; return; }
  if (!state.collections.length) { grid.innerHTML = `<div class="empty"><div class="glyph">${icon('layers', 28)}</div><h2>No collections yet</h2><div>Create one, then add a document from Ingest.</div><div class="row">${canWrite() ? '<button class="btn primary" data-on-click="[[&quot;openCreate&quot;]]">Create collection</button>' : ''}</div></div>`; return; }
  if (!list.length) { grid.innerHTML = `<div class="empty"><h2>Nothing matches</h2><div>Clear the filter to see all ${state.collections.length}.</div></div>`; return; }
  // Most recently updated first, ten a page. The filter box above is the find.
  const sorted = [...list].sort((a, b) => ((state.health[b.name] || {}).updated_at || b.updated_at || '').localeCompare((state.health[a.name] || {}).updated_at || a.updated_at || ''));
  const pages = Math.max(1, Math.ceil(sorted.length / PAGE)); pg.page = Math.min(pg.page, pages - 1);
  const from = pg.page * PAGE, shown = sorted.slice(from, from + PAGE);
  const gated = !!(state.policies && state.policies.gated);
  const needs = state.collections.filter((c) => ['Rebuild advised', 'Model missing'].includes(stateOf(c.name).text)).length;
  const row = (c) => {
    const s = stateOf(c.name); const h = state.health[c.name] || {};
    const action = !can('collection.maintain') ? '' : s.text === 'Rebuild advised' ? `<button class="btn sm" ${on('click', ['rebuild', c.name])} data-stop>Rebuild</button>`
      : s.text === 'Model missing' ? `<button class="btn sm" data-on-click="[[&quot;copyText&quot;,&quot;vectrixdb download-models&quot;]]" data-stop>Copy download command</button>` : '';
    return `<div class="tr ${state.guest ? '' : 'click'} t-collist" ${state.guest ? '' : on('click', ['go', '#/collections/' + encodeURIComponent(c.name)])}>${mono(c.name)}<span class="row" style="gap: 6px">${pill(s.text, s.kind)}${action}</span><span>${gated ? whoSearches(c.name) : esc(modeOf(c))}</span>${mono(h.policied ? 'withheld' : fmtNum(h.count ?? c.count))}<span class="muted">${ago(h.updated_at || c.updated_at || c.created_at)}</span></div>`;
  };
  grid.innerHTML = `<div class="head"><h2>${countOf(state.collections.length, 'collection')}</h2><span class="faint">most recently updated first</span></div>
    ${pager('cols', { find: false, total: sorted.length, from, count: shown.length })}
    <div class="table"><div class="tr head t-collist"><span>Name</span><span>State</span><span>${gated ? 'Who can search it' : 'Mode'}</span><span>Chunks</span><span>Updated</span></div>${shown.map(row).join('')}</div>
    ${needs ? `<div class="faint">${needs === 1 ? 'One collection needs' : `${needs} collections need`} a human: a rebuild, or a model to download.</div>` : ''}`;
}
PAGERS.cols = () => renderCollections();
function setColFilter(f) { collectionFilter = f; renderCollections(); }

async function rebuild(name) {
  try { const d = await api(`/api/v1/collections/${encodeURIComponent(name)}/rebuild`, { method: 'POST' }); snack(`Rebuilt ${name}: ${countOf(d.vectors, 'vector')}`); refresh(); }
  catch (e) { snack(e.message); }
}

/* ---------------------------------------------------------- collection */
const TABS = ['overview', 'points', 'policy', 'builds', 'quality'];
async function loadCollection() {
  // A new collection starts its Points at the first page, with no find.
  if (state.pages.pointsFor !== state.collection) { state.pages.points = { q: '', page: 0 }; state.pages.pointsFor = state.collection; }
  if (!state.collections.length) await loadCollectionList();
  await loadModels();
  const name = state.collection;
  const c = state.collections.find((x) => x.name === name);
  if (!c) { $('c-header').innerHTML = `<div class="notice bad">${icon('x')}<div>No collection called <b class="mono">${esc(name)}</b>. <a href="#/collections">Back to collections</a></div></div>`; return; }
  let h = state.health[name]; if (!h) { try { h = state.health[name] = await api(`/api/v1/collections/${encodeURIComponent(name)}/health`); } catch (e) { h = {}; } }
  if (state.signinOn && !state.policies) await loadPolicies();
  const s = stateOf(name); const mode = modeOf(c);
  $('c-crumbs').innerHTML = `<a href="#/collections">Collections</a><span class="faint">/</span><span style="color: var(--t1)">${esc(name)}</span>`;
  $('c-header').innerHTML = `<div class="card chead">
    <div class="chead-glyph">${icon('layers', 24)}</div>
    <div class="chead-title row"><span class="chead-name mono">${esc(name)}</span>${pill(s.text, s.kind)}<span class="pill" title="${esc(`${h.embedding_model || 'the built-in model'}, ${h.text_language || 'en'}`)}">${esc(modeOf(c))}</span></div>
    <div class="chead-desc muted">${esc(c.description || h.description || 'No description.')}</div>
    <div class="chead-facts facts"><span class="k">Searches</span><span class="row">${searchWays(c, h)}</span><span class="k">Built with</span><span class="row">${builtWith(c, h)}</span>${ownTags(c).length ? `<span class="k">Tags</span><span class="row">${ownTags(c).map(tag).join('')}</span>` : ''}</div>
    ${can('search') || can('collection.delete') || can('content.write') ? `<div class="chead-act">${can('search') ? `<button class="btn primary" ${on('click', ['searchIn', name])}>${icon('search', 15)} Search</button>` : ''}${can('content.write') && state.policies && state.policies.gated ? `<button class="btn" ${on('click', ['openFiles'])}>${icon('upload', 15)} Add files</button>` : ''}${can('collection.delete') ? `<button class="btn danger" ${on('click', ['confirmDelete', name])}>Delete</button>` : ''}</div>` : ''}
  </div>`;
  // A guest sees what a collection is and searches it. Its points, policy, builds and settings are for people signed in.
  // A tab somebody's role does not show them, reached by its address, opens the collection's overview instead.
  if (state.guest || !TABS.includes(state.tab) || (state.tab === 'quality' && !can('content.read'))) state.tab = 'overview';
  TABS.forEach((t) => { if (state.guest) $(`ctab-${t}`).hidden = t !== 'overview'; $(`ctab-${t}`).classList.toggle('on', t === state.tab); $(`cpanel-${t}`).classList.toggle('on', t === state.tab); });
  await loadCollectionTab();
}
function setTab(t) { go(`#/collections/${encodeURIComponent(state.collection)}/${t}`); }
function refusal(what) {
  return `<div class="notice warn">${icon('lock')}<div><b>This collection carries an entitlement policy</b>, and this API resolves no principals, so it cannot say ${what} to any one reader. Serve it from a tier that resolves principals, or read it from Python:</div></div>
  <pre>from vectrixdb import Vectrix

db = Vectrix("${esc(state.collection)}")
hits = db.search("your query", principal=me)   # me is the resolved principal</pre>`;
}
/* A tab the collection's policy keeps from this person says so, as a search does, and never stays blank. */
function tabRefused(panel, name, e) {
  const data = e && e.body && e.body.data;
  $(panel).innerHTML = e && e.status === 403 && data && data.code ? restrictedResults(name, data) : `<div class="notice bad">${icon('x')}<div>${esc((e && e.message) || 'This could not be loaded.')}</div></div>`;
}
async function loadCollectionTab() {
  const name = state.collection; const enc = encodeURIComponent(name); const h = state.health[name] || {}; const t = state.tab;
  const c = state.collections.find((x) => x.name === name) || {};
  if (t === 'overview') {
    if (h.policied) { $('cpanel-overview').innerHTML = `<div class="grid metrics">${metric('Policy', short(h.policy_fingerprint, 10), 'fingerprint')}${metric('Last write', h.updated_at ? ago(h.updated_at) : 'Never')}</div>${refusal('how much is in it')}`; return; }
    const tomb = Math.round((h.tombstone_ratio || 0) * 100);
    // The language, the model and the mode are tags in the header. A tile is
    // for a number somebody reads at a glance, and four of them fill one row.
    const wrote = h.updated_at || c.updated_at;
    $('cpanel-overview').innerHTML = `<div class="grid metrics">
      ${metric('Chunks', fmtNum(h.count), `${c.dimension || 384} dims · ${c.metric || 'cosine'}`)}
      ${metric('Index', h.state === 'rebuild' ? 'Rebuild' : 'Healthy', `${tomb}% tombstones${h.state === 'rebuild' ? ', rebuild advised' : ''}`, h.state === 'rebuild' ? 'warn' : 'ok')}
      ${c.shared_store ? metric('Kept in', 'Shared store', 'read by every instance') : metric('On disk', fmtBytes(c.size_bytes), state.info?.storage_backend || 'sqlite')}
      ${metric('Last write', wrote ? ago(wrote) : 'Never', wrote ? new Date(wrote).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : 'nothing written yet')}
    </div>
    <div class="grid lay-one" id="c-growth" hidden></div>
    <div class="grid two">
      <div class="card"><div class="head"><h2>Use this collection</h2></div>
        <pre id="c-snippet">from vectrixdb import Vectrix

db = Vectrix("${esc(name)}", mode="${modeOf(c)}"${h.text_language && h.text_language !== 'en' ? `, text_language="${esc(h.text_language)}"` : ''})
hits = db.search("your query", explain=True)
print(hits.top.text)</pre>
        <div class="row"><button class="btn sm" data-on-click="[[&quot;copyFrom&quot;,&quot;c-snippet&quot;]]">${icon('copy', 14)} Copy</button><button class="btn sm" ${on('click', ['consoleSearch', name])}>${icon('terminal', 14)} REST</button></div>
      </div>
      <div class="card"><div class="head"><h2>Recent activity</h2></div><div class="table" id="c-activity">${h.updated_at ? `<div class="tr t-split"><span>Last write</span><span class="muted">${ago(h.updated_at)}</span></div>` : ''}${c.created_at ? `<div class="tr t-split"><span>Created</span><span class="muted">${ago(c.created_at)}</span></div>` : ''}${h.index_build_id ? `<div class="tr t-split"><span>Index build</span><span>${idChip(h.index_build_id)}</span></div>` : ''}${h.advice ? `<div class="notice warn">${icon('alert')}<div>${esc(h.advice)} <a href="#/collections/${enc}/builds">Rebuild</a></div></div>` : ''}</div></div>
    </div>`;
    // Growth comes after the page is up: it reads every chunk's time of writing, and the tiles should not wait for it.
    if (!state.guest) api(`/api/v1/collections/${enc}/growth?days=30`, { quiet: true }).then((g) => { const holder = $('c-growth'); if (!holder || state.collection !== name || !(g.before + g.written.reduce((a, b) => a + b, 0))) return; holder.hidden = false; trGrowth(holder, g); }).catch(() => {});
  } else if (t === 'points') {
    if (h.policied) { $('cpanel-points').innerHTML = refusal('which documents exist'); return; }
    try { await loadPoints(); } catch (e) { tabRefused('cpanel-points', name, e); }
  } else if (t === 'policy') {
    await loadPolicies();
    const gated = !!(state.policies && state.policies.gated);
    const who = gated ? whoCards(name, state.policies) : `<div class="card"><div class="head"><h2>Who may search this collection</h2></div><div class="notice">${icon('info')}<div>Nothing is gated on this server. Set VECTRIXDB_COLLECTION_STORE to where each collection's record is kept, and who may search each collection is decided here.</div></div></div>`;
    let rules = '';
    if (h.policied) {
      // A per-document policy declared in code when the collection was made: shown, never edited here.
      try {
        const d = await api(`/api/v1/collections/${enc}/policy`);
        const list = (d.policy && d.policy.rules) || [];
        rules = `<div class="card"><div class="head"><h2>Per-document rules · fingerprint ${mono(d.fingerprint)}</h2><button class="btn sm" ${on('click', ['copyText', JSON.stringify(d.policy, null, 2)])}>${icon('copy', 14)} Copy as JSON</button></div>
        <div class="table"><div class="tr head t-rules"><span>Predicate</span><span>Document field</span><span>Principal key</span><span>Kind</span></div>
        ${list.map((r) => `<div class="tr t-rules">${mono(r.kind)}${mono(r.doc)}${mono(r.principal)}<span>${pill(r.scope ? 'scope' : 'redact', r.scope ? 'acc' : '')}</span></div>`).join('')}</div>
        <div class="faint">${esc(d.note || '')}${d.policy && d.policy.label ? ' · label ' + esc(d.policy.label) : ''} Declared in code when the collection was made; enforcement lives below the API.</div></div>`;
      } catch (e) { rules = `<div class="notice warn">${icon('lock')}<div>${esc(e.message)}</div></div>`; }
    }
    $('cpanel-policy').innerHTML = `<div class="grid two">${who}</div>${rules}`;
    if (gated) renderWho(`s-${name}`);
  } else if (t === 'builds') {
    if (h.policied) { $('cpanel-builds').innerHTML = refusal('which builds wrote how much'); return; }
    try {
      const d = await api(`/api/v1/collections/${enc}/builds`);
      const line = d.quality_threshold || 0.78;
      // Newest first in the table; the chart above it reads oldest first.
      d.builds.sort((a, b) => (a.written_at || '') < (b.written_at || '') ? 1 : -1);
      state.builds = { list: d.builds, line };
      $('cpanel-builds').innerHTML = `<div class="grid lay-one" id="c-builds-chart"></div><div class="grid two"><div class="card"><div class="head"><h2>Index builds</h2><div class="row"><button class="btn sm" ${on('click', ['rebuild', name])}>Rebuild index</button></div></div>
        <div class="muted">Every mutation mints a build id and every chunk carries the one that stored it. This lists builds that still have chunks; the audit trail is the history.</div>
        <div id="builds-table">${buildsTableHtml()}</div>
        ${d.unstamped_chunks ? `<div class="faint">${countOf(d.unstamped_chunks, 'chunk')} ${d.unstamped_chunks === 1 ? 'carries' : 'carry'} no build stamp: written before 2.2, or through the REST API before this build stamped them.</div>` : ''}</div>
        <div class="card"><div class="head"><h2>Provenance of a chunk</h2></div><div class="row"><input id="prov-id" placeholder="chunk id" style="flex-grow:1" data-on-keydown="[[&quot;lookupProvenance&quot;]]" data-key="Enter"><button class="btn" data-on-click="[[&quot;lookupProvenance&quot;]]">Look up</button></div><div id="prov-out" class="faint">Which build stored it, the source and document version, the chunking, and its quality score.</div></div></div>`;
      trBuilds($('c-builds-chart'), d.builds, line);
    } catch (e) { $('cpanel-builds').innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(e.message)}</div></div>`; }
  } else if (t === 'quality') {
    if (h.policied) { $('cpanel-quality').innerHTML = refusal('how many chunks are below the line'); return; }
    try {
      state.pages.quality = { q: '', page: 0 };
      const d = await api(`/api/v1/collections/${enc}/quality?text=false&worst=${PAGE}`);
      const max = Math.max(1, ...d.bins);
      $('cpanel-quality').innerHTML = `<div class="grid metrics">${metric('Below the line', fmtNum(d.below), d.scored ? `${Math.round(d.below / d.scored * 100)}% of scored chunks` : 'nothing scored', d.below ? 'warn' : '')}${metric('Threshold', d.threshold.toFixed(2), 'from the labelled set')}${metric('Scored', fmtNum(d.scored), `${fmtNum(d.unscored)} unscored`)}</div>
        <div class="grid two"><div class="card"><div class="head"><h2>Extraction quality</h2></div>
        ${d.scored ? `<div class="bars">${d.bins.map((n, i) => `<i class="${(i + 1) / 20 <= d.threshold ? 'low' : ''}" style="height:${Math.round(n / max * 100)}%" title="${(i / 20).toFixed(2)}–${((i + 1) / 20).toFixed(2)}: ${n}"></i>`).join('')}</div><div class="row" style="justify-content: space-between" class="faint"><span class="faint">0.0</span><span class="faint" style="color: var(--warn)">${d.threshold.toFixed(2)}, the line</span><span class="faint">1.0</span></div>` : `<div class="notice">${icon('info')}<div>No quality scores. add_document() stamps every chunk with one; a plain add() does not.</div></div>`}
        <div class="faint">Measured on 613 paragraphs and their OCR-degraded twins: precision 0.957, recall 0.987 at 0.78.</div></div>
        <div id="c-low">${lowChunksCard(d)}</div></div>`;
    } catch (e) { tabRefused('cpanel-quality', name, e); }
  }
}
/* The chunks below the quality line, lowest first, ten a page: the server
   sorts the whole collection and sends the page asked for. */
function lowChunksCard(d) {
  const low = d.worst.filter((w) => w.below_line); const from = d.offset || 0;
  return `<div class="card"><div class="head"><h2>${d.below ? `Below the quality line (${fmtNum(d.below)})` : 'Below the quality line'}</h2></div>
    ${!d.scored ? '<div class="muted">Nothing scored yet.</div>' : !d.below ? `<div class="notice ok">${icon('check')}<div><b>No chunk is below the line.</b> The lowest scores ${d.worst.length ? d.worst[0].quality.toFixed(2) : ''}, and the line is ${d.threshold.toFixed(2)}.</div></div>`
    : `${pager('quality', { find: false, total: d.below, from, count: low.length })}${low.map((w) => `<div class="chunk-row"><div class="row" style="justify-content: space-between">${mono(w.id)}${pill(w.quality.toFixed(2), 'warn')}</div><div class="row" style="justify-content: space-between"><span class="muted">${w.reasons && w.reasons.length ? esc(w.reasons.join('; ')) : 'low on several signals at once'}</span>${can('content.read') ? revealButton(state.collection, w.id) : ''}</div></div>`).join('')}`}</div>`;
}
PAGERS.quality = async (q, page) => {
  const el = $('c-low'); if (!el || !state.collection) return;
  try { el.innerHTML = lowChunksCard(await api(`/api/v1/collections/${encodeURIComponent(state.collection)}/quality?text=false&worst=${PAGE}&offset=${page * PAGE}`)); } catch (e) { snack(e.message); }
};
/* The index builds, newest first, ten a page. */
function buildsTableHtml() {
  const { list, line } = state.builds; const pg = pageState('builds');
  const pages = Math.max(1, Math.ceil(list.length / PAGE)); pg.page = Math.min(pg.page, pages - 1);
  const from = pg.page * PAGE, shown = list.slice(from, from + PAGE);
  const reads = (b) => b.quality === null || b.quality === undefined ? '<span class="faint">–</span>' : b.quality < line ? `<span class="pill bad" title="${countOf(b.low, 'chunk')} of this build under the line at ${line}">${b.quality.toFixed(2)}</span>` : mono(b.quality.toFixed(2));
  return `${pager('builds', { find: false, total: list.length, from, count: shown.length })}<div class="table"><div class="tr head t-builds"><span>Build</span><span>Chunks</span><span>Quality</span><span></span></div>
    ${shown.length ? shown.map((b) => `<div class="tr t-builds"><span>${idChip(b.build_id)}</span>${mono(fmtNum(b.chunks))}<span>${reads(b)}</span><span>${b.current ? pill('current', 'acc') : ''}</span></div>`).join('') : `<div class="tr t-one"><span class="muted">No stamped chunks yet</span></div>`}</div>`;
}
PAGERS.builds = () => { const el = $('builds-table'); if (el && state.builds) el.innerHTML = buildsTableHtml(); };

/* --------------------------------------- who may retrieve a collection */
/* A collection's policy: who gets an answer from it, checked on the server
   before anything is searched. Two cards: the sign-in token, security
   groups by object id read from what the identity provider signs into the
   token; and the membership store, a list kept with the collection of
   specific people by email, or a domain for everyone there. Pick a card and
   its list appears under it. Past five rows the list scrolls in its own
   box, and a pasted batch becomes rows. No policy means nobody. One editor,
   keyed, so the Policy tab and the page that makes a collection can each
   hold one. */
const PLACEHOLDER_GROUP = '00000000-0000-0000-0000-000000000000';
/* The server's words, said before it is asked: a group alone would let everyone in it search. */
const NEEDS_A_LIST = 'Add the people who may search. A group alone would let everyone in it search.';
const WHO = {};
const WHO_SHORT = 5;
function newWho(policy) {
  const who = { method: '', token: [{ id: '', name: '' }], people: [{ who: '' }], store: [{ who: '' }], paste: false, pasted: '' };
  if (policy && policy.method) {
    who.method = policy.method === 'people' ? 'store' : policy.method === 'groups' ? 'token' : policy.method;
    if (who.method === 'token') who.token = (policy.allow || []).map((g) => ({ id: g.id || '', name: g.name || '' }));
    if (who.method === 'token') who.people = (policy.people || []).map((p) => ({ who: p.email || '' }));
    if (who.method === 'store') who.store = (policy.allow || []).map((p) => ({ who: p.email || p.domain || '' }));
    if (!who.token.length) who.token = [{ id: '', name: '' }];
    if (!who.people.length) who.people = [{ who: '' }];
    if (!who.store.length) who.store = [{ who: '' }];
  }
  return who;
}
function andList(items) { return items.length > 1 ? `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}` : (items[0] || ''); }
function describePolicy(p) {
  if (!p || !p.method) return 'nobody';
  const method = p.method === 'people' ? 'store' : p.method === 'groups' ? 'token' : p.method;
  const n = (p.allow || []).length;
  if (method === 'token') {
    const people = (p.people || []).map((e) => e.email).filter(Boolean);
    return `${andList((p.allow || []).map((g) => g.name || g.id))}, narrowed to ${people.length ? people.join(', ') : 'nobody yet'}`;
  }
  return (p.allow || []).map((e) => e.email || `everyone at ${e.domain}`).join(', ');
}
function whoLabel(p) {
  if (!p || !p.method) return 'nobody';
  const n = (p.allow || []).length;
  if (p.method === 'store' || p.method === 'people') { const d = (p.allow || []).filter((e) => e.domain).length; return d ? `${n - d} people, ${d} domain${d === 1 ? '' : 's'}` : `${n} ${n === 1 ? 'person' : 'people'}`; }
  return `${n} group${n === 1 ? '' : 's'}`;
}
function whoCount(who) {
  const rows = who.store.map((r) => r.who.trim()).filter(Boolean);
  const domains = rows.filter((r) => !r.includes('@')).length;
  const people = rows.length - domains;
  return `${people} ${people === 1 ? 'person' : 'people'}${domains ? ` and ${domains} domain${domains === 1 ? '' : 's'}` : ''}`;
}
function policyFrom(who) {
  if (!who.method) throw new Error('Pick who can retrieve: the sign-in token, or the membership store');
  if (who.method === 'token') {
    const allow = who.token.map((g) => ({ id: g.id.trim(), name: g.name.trim() })).filter((g) => g.id);
    if (!allow.length) throw new Error('Add at least one security group, by its object id');
    const people = who.people.map((p) => p.who.trim().toLowerCase()).filter(Boolean).map((email) => ({ email }));
    if (!people.length) throw new Error(NEEDS_A_LIST);
    return { method: 'token', allow: allow.map((g) => (g.name ? g : { id: g.id })), people };
  }
  const allow = who.store.map((p) => p.who.trim().toLowerCase().replace(/^@/, '')).filter(Boolean).map((p) => (p.includes('@') ? { email: p } : { domain: p }));
  if (!allow.length) throw new Error('Add at least one email, or a domain for everyone there');
  return { method: 'store', allow };
}
/* A pasted batch: commas, spaces and line breaks all split it; what is on the list already is left out. */
function whoPasteKind(who) { return who.method === 'token' ? 'people' : 'store'; }
function whoParsePaste(who) {
  const kind = whoPasteKind(who);
  const have = new Set(who[kind].map((r) => r.who.trim().toLowerCase().replace(/^@/, '')).filter(Boolean));
  const found = [...new Set(String(who.pasted || '').split(/[\s,;]+/).map((s) => s.trim().toLowerCase().replace(/^@/, '')).filter((s) => s && (s.includes('@') ? /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s) : kind === 'store' && /^[^@\s]+\.[^@\s]+$/.test(s))))];
  const fresh = found.filter((s) => !have.has(s));
  return { found, fresh };
}
function whoPasteWords(who) {
  const { found, fresh } = whoParsePaste(who);
  if (!found.length) return whoPasteKind(who) === 'people' ? 'Paste work emails, separated by commas or line breaks' : 'Paste emails, or domains, separated by commas or line breaks';
  const already = found.length - fresh.length;
  return `${found.length} found: ${fresh.length} new${already ? `, ${already} already on the list` : ''}`;
}
function whoEditor(key, who, may = true) {
  const off = may ? '' : 'disabled';
  const card = (value, title, words) => `<button type="button" class="who-card ${who.method === value ? 'on' : ''}" aria-pressed="${who.method === value}" ${on('click', ['pickWho', key, value])} ${off}><b>${title}</b><span>${words}</span></button>`;
  const remove = (kind, i) => (may ? `<button class="btn sm" type="button" aria-label="Remove" ${on('click', ['dropWhoRow', key, kind, i])}>${icon('x', 12)}</button>` : '');
  let body = '';
  if (who.method === 'token') {
    const { fresh } = whoParsePaste(who);
    const listed = who.people.map((p) => p.who.trim()).filter(Boolean).length;
    body = `<div class="who-label">Groups</div>
      <div class="who-list ${who.token.length > WHO_SHORT ? 'long' : ''}" data-kind="token">${who.token.map((g, i) => `<div class="who-row"><input class="mono" value="${esc(g.id)}" placeholder="${PLACEHOLDER_GROUP}" aria-label="Group object id" ${on('input', ['whoInput', key, 'token', i, 'id', VALUE])} ${off}><input value="${esc(g.name)}" placeholder="Name, if you like" aria-label="Name" ${on('input', ['whoInput', key, 'token', i, 'name', VALUE])} ${off}>${remove('token', i)}</div>`).join('')}</div>
      ${may ? `<div class="row"><button class="btn sm" type="button" ${on('click', ['addWhoRow', key, 'token'])}>${icon('plus', 12)} Add a group</button></div>` : ''}
      <div class="row between"><span class="who-label">People on the list</span><span class="faint">${listed ? esc(countOf(listed, 'person', 'people')) : 'Nobody on the list yet.'}</span></div>
      <div class="who-list ${who.people.length > WHO_SHORT ? 'long' : ''}" data-kind="people">${who.people.map((p, i) => `<div class="who-row one"><input class="mono" type="email" value="${esc(p.who)}" placeholder="ama@company.com" aria-label="Work email" ${on('input', ['whoInput', key, 'people', i, 'who', VALUE])} ${off}>${remove('people', i)}</div>`).join('')}</div>
      ${may ? `<div class="row"><button class="btn sm" type="button" ${on('click', ['addWhoRow', key, 'people'])}>${icon('plus', 12)} Add one</button><button class="btn sm ${who.paste ? 'on' : ''}" type="button" aria-pressed="${!!who.paste}" ${on('click', ['whoPasteToggle', key])}>${icon('copy', 12)} Paste a list</button></div>` : ''}
      ${may && who.paste ? `<div class="who-paste"><textarea class="mono" rows="3" aria-label="Paste work emails" placeholder="akua@company.com, kwabena@company.com" ${on('input', ['whoPasteInput', key, VALUE])}>${esc(who.pasted)}</textarea><div class="row between"><span class="faint" id="who-paste-words-${esc(key)}">${esc(whoPasteWords(who))}</span><button class="btn sm" type="button" ${on('click', ['whoPasteAdd', key])} ${fresh.length ? '' : 'disabled'}>Add ${fresh.length || ''}</button></div></div>` : ''}
      <div class="faint">In one of these groups, from the sign-in token, and on the list by work email. Anyone else is refused, and the refusal is logged. Not everyone in a group may search: the list decides.</div>`;
  } else if (who.method === 'store') {
    const { fresh } = whoParsePaste(who);
    body = `<div class="row between faint"><span>${esc(whoCount(who))}</span>${who.store.length > WHO_SHORT ? '<span>scroll for the rest</span>' : ''}</div>
      <div class="who-list ${who.store.length > WHO_SHORT ? 'long' : ''}" data-kind="store">${who.store.map((p, i) => `<div class="who-row one"><input class="mono" value="${esc(p.who)}" placeholder="ama@company.com, or company.com for everyone there" aria-label="Email or domain" ${on('input', ['whoInput', key, 'store', i, 'who', VALUE])} ${off}>${remove('store', i)}</div>`).join('')}</div>
      ${may ? `<div class="row"><button class="btn sm" type="button" ${on('click', ['addWhoRow', key, 'store'])}>${icon('plus', 12)} Add one</button><button class="btn sm ${who.paste ? 'on' : ''}" type="button" aria-pressed="${!!who.paste}" ${on('click', ['whoPasteToggle', key])}>${icon('copy', 12)} Paste a list</button></div>` : ''}
      ${may && who.paste ? `<div class="who-paste"><textarea class="mono" rows="3" aria-label="Paste emails or domains" placeholder="akua@company.com, kwabena@company.com" ${on('input', ['whoPasteInput', key, VALUE])}>${esc(who.pasted)}</textarea><div class="row between"><span class="faint" id="who-paste-words-${esc(key)}">${esc(whoPasteWords(who))}</span><button class="btn sm" type="button" ${on('click', ['whoPasteAdd', key])} ${fresh.length ? '' : 'disabled'}>Add ${fresh.length || ''}</button></div></div>` : ''}
      <div class="faint">Person A, person B, person C: as long a list as you like, just the emails. A domain lets everyone there in.</div>`;
  } else {
    body = '<div class="faint">Required. Pick one, and its list appears here.</div>';
  }
  return `<div class="who" id="who-${esc(key)}">
    <div class="who-cards">${card('token', 'The sign-in token', 'Security groups, by object id, narrowed to the people you list.')}${card('store', 'The membership store', 'A list kept with the collection: specific people by email, or everyone at a domain.')}</div>
    <div class="who-body">${body}</div>
  </div>`;
}
function renderWho(key) { const holder = $(`who-holder-${key}`); if (holder) holder.innerHTML = whoEditor(key, WHO[key], !holder.dataset.readonly); }
function pickWho(key, method) { WHO[key].method = method; renderWho(key); }
function blankWhoRow(kind) { return kind === 'token' ? { id: '', name: '' } : { who: '' }; }
function addWhoRow(key, kind) {
  WHO[key][kind].push(blankWhoRow(kind)); renderWho(key);
  const rows = document.querySelectorAll(`#who-${key} .who-list[data-kind="${kind}"] .who-row input:first-child`); if (rows.length) { rows[rows.length - 1].focus(); rows[rows.length - 1].scrollIntoView({ block: 'nearest' }); }
}
function dropWhoRow(key, kind, i) { WHO[key][kind].splice(i, 1); if (!WHO[key][kind].length) WHO[key][kind].push(blankWhoRow(kind)); renderWho(key); }
function whoInput(key, kind, i, field, value) { if (WHO[key][kind][i]) WHO[key][kind][i][field] = value; }
function whoPasteToggle(key) { WHO[key].paste = !WHO[key].paste; renderWho(key); if (WHO[key].paste) { const t = document.querySelector(`#who-${key} .who-paste textarea`); if (t) t.focus(); } }
function whoPasteInput(key, value) {
  WHO[key].pasted = value;
  const words = $(`who-paste-words-${key}`); if (words) words.textContent = whoPasteWords(WHO[key]);
  const { fresh } = whoParsePaste(WHO[key]);
  const btn = document.querySelector(`#who-${key} .who-paste .btn`); if (btn) { btn.disabled = !fresh.length; btn.textContent = `Add ${fresh.length || ''}`.trim(); }
}
function whoPasteAdd(key) {
  const who = WHO[key]; const { fresh } = whoParsePaste(who);
  if (!fresh.length) return;
  const kind = whoPasteKind(who);
  who[kind] = who[kind].filter((r) => r.who.trim()).concat(fresh.map((s) => ({ who: s })));
  who.pasted = ''; who.paste = false;
  renderWho(key);
  snack(`${fresh.length} added to the list`);
}
function whoCards(name, vis) {
  const v = vis.collections[name] || {};
  const may = can('collection.share');
  if (!WHO[`s-${name}`]) WHO[`s-${name}`] = newWho(v.policy);
  const now = v.policy ? `Right now: ${describePolicy(v.policy)}.` : 'Right now: nobody. Without a policy, nobody can retrieve from it.';
  return `<div class="card"><div class="head"><h2>Who may search this collection</h2>${v.policy ? whoTag(v) : pill('nobody yet', 'warn')}</div>
    <div class="muted">${esc(now)} Checked on the server before anything is searched. Anyone else who searches is refused, and the refusal is logged.</div>
    <div id="who-holder-s-${esc(name)}" ${may ? '' : 'data-readonly="1"'}></div>
    <div id="who-err-s-${esc(name)}"></div>
    ${may ? `<div class="row"><button class="btn primary" ${on('click', ['saveWho', name])}>Save</button><span class="faint">Recorded in the access log under your name, and in force within thirty seconds.</span></div>` : '<p class="faint" style="margin: 0">Only an admin can change who retrieves from a collection.</p>'}
  </div>
  <div class="card"><div class="head"><h2>Check someone</h2></div>
    <div class="muted">Whether an address would get in, and why, without searching anything. For the sign-in token, give the groups to try them with.</div>
    <div class="grid two"><label class="field">Email<input id="chk-email" placeholder="ama@company.com" ${on('keydown', ['checkSomeone', name])} data-key="Enter"></label><label class="field">Groups, comma separated<input id="chk-groups" class="mono" placeholder="${PLACEHOLDER_GROUP}" ${on('keydown', ['checkSomeone', name])} data-key="Enter"></label></div>
    <div class="row"><button class="btn" ${on('click', ['checkSomeone', name])}>Check</button></div>
    <div id="chk-out"></div>
  </div>`;
}
/* Not saved, and why, where the Save button is: a snack would be gone before it was read. */
function whoNotSaved(name, message) {
  const el = $(`who-err-s-${name}`);
  if (el) el.innerHTML = message ? `<div class="notice bad" role="alert">${icon('alert')}<div><b>Not saved.</b> ${esc(message)}</div></div>` : '';
  else if (message) snack(message);
}
async function saveWho(name) {
  let policy;
  whoNotSaved(name, '');
  try { policy = policyFrom(WHO[`s-${name}`]); } catch (e) { whoNotSaved(name, e.message); return; }
  try {
    await api(`/api/v1/collections/${encodeURIComponent(name)}/policy`, { method: 'PUT', body: JSON.stringify({ policy }) });
    snack(`${name}: ${describePolicy(policy)} can retrieve from it`);
    delete WHO[`s-${name}`]; state.policies = null; loadCollectionTab();
  } catch (e) { whoNotSaved(name, e.message); }
}
async function checkSomeone(name) {
  const email = $('chk-email').value.trim(); const groups = $('chk-groups').value.split(',').map((g) => g.trim()).filter(Boolean);
  if (!email) { $('chk-out').innerHTML = `<div class="notice warn">${icon('info')}<div>Give an address to check.</div></div>`; return; }
  try {
    const d = await api('/api/v1/access/check', { method: 'POST', body: JSON.stringify({ collection: name, email, ...(groups.length ? { groups } : {}) }) });
    $('chk-out').innerHTML = `<div class="notice ${d.allowed ? 'ok' : 'warn'}">${icon(d.allowed ? 'check' : 'lock')}<div><b>${d.allowed ? 'Allowed' : 'Refused'}</b> ${mono(d.code)}<br>${esc(d.because)}</div></div>`;
  } catch (e) { $('chk-out').innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(e.message)}</div></div>`; }
}

/* ---------------------------------------------- a chunk's provenance */
async function lookupProvenance() {
  const id = $('prov-id').value.trim(); if (!id) return;
  try {
    const d = await api(`/api/v1/collections/${encodeURIComponent(state.collection)}/provenance/${encodeURIComponent(id)}?text=false`);
    const rows = [['Present now', d.present ? 'yes' : 'no'], ['Stored by build', d.build_id ? { html: idChip(d.build_id) } : null], ['Document', d.document_id], ['Document version', d.document_version], ['Source', d.source], ['Chunk', d.chunk_index], ['Chunking', d.chunk_strategy ? `${d.chunk_strategy} ${d.chunk_size || ''}/${d.chunk_overlap || ''}` : null], ['Quality', d.quality]];
    $('prov-out').innerHTML = `<div class="table">${rows.filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => `<div class="tr t-kv"><span class="muted">${esc(k)}</span>${v && v.html ? `<span>${v.html}</span>` : mono(v)}</div>`).join('')}</div>${d.present && can('content.read') ? `<div class="row" style="padding-top: 8px">${revealButton(state.collection, id)}</div>` : ''}`;
  } catch (e) { $('prov-out').innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(e.message)}</div></div>`; }
}

/* -------------------------------------------------------------- points */
const PAGE_SIZE = PAGE;
async function loadPoints() {
  const enc = encodeURIComponent(state.collection);
  const pg = pageState('points');
  // The listing says where each chunk came from and how clean it is, and not
  // a word of what it says. A chunk's text is one request, for that chunk,
  // made when somebody asks, and written to the access log under their name.
  const d = await api(`/api/v1/collections/${enc}/points?${pageQuery('points')}&index=true`);
  const rows = d.rows || (d.ids || []).map((id) => ({ id }));
  const total = d.total || 0; const reads = can('content.read');
  $('cpanel-points').innerHTML = `<div class="card"><div class="head"><h2>${countOf(total, 'point')}</h2><div class="row">${canWrite() ? `<button class="btn sm" data-on-click="[[&quot;go&quot;,&quot;#/ingest&quot;]]">${icon('upload', 14)} Add</button>` : ''}</div></div>
    ${reads ? '' : `<div class="notice info">${icon('lock')}<div><b>Your role shows that chunks exist, not what they say.</b> Text and metadata are for operators and admins.</div></div>`}
    ${pager('points', { placeholder: 'Find by id or source', q: pg.q, total, from: pg.page * PAGE, count: rows.length })}
    <div class="table"><div class="tr head t-points"><span>Id</span><span>Quality</span><span>Source</span><span></span></div>
    ${rows.map((r) => { const q = r.quality; return `<div class="tr t-points">${mono(r.id)}<span>${q !== undefined && q !== null ? pill(Number(q).toFixed(2), q < 0.78 ? 'warn' : '') : '<span class="faint">–</span>'}</span>${mono(r.source || '–', 'faint')}<span class="row" style="justify-content: flex-end">${reads ? revealButton(state.collection, r.id) : ''}</span></div>`; }).join('') || `<div class="tr t-one"><span class="muted">${pg.q.trim() ? 'No point is called that.' : 'No points yet.'}</span></div>`}</div>
    <div class="faint">${reads ? 'Text is shown one chunk at a time, when asked for, and each time is recorded.' : 'Vectors and text are never listed.'}</div></div>`;
}
PAGERS.points = () => loadPoints().catch((e) => tabRefused('cpanel-points', state.collection, e));

/* One control, used wherever a chunk is listed: Points, Quality, provenance,
   search. It opens the chunk under the row it sits in, and closes it again. */
function revealButton(collection, id) {
  return `<button class="btn sm reveal" aria-expanded="false" ${on('click', ['toggleChunk', THIS, String(collection), String(id)])}>${icon('book', 14)} <span>Show text</span></button>`;
}
async function toggleChunk(btn, collection, id) {
  const host = btn.closest('.tr, .result, .chunk-row') || btn.parentElement;
  const open = host.nextElementSibling && host.nextElementSibling.classList.contains('chunk-open') ? host.nextElementSibling : null;
  const label = btn.querySelector('span');
  if (open) { open.remove(); btn.setAttribute('aria-expanded', 'false'); if (label) label.textContent = 'Show text'; return; }
  btn.disabled = true;
  try {
    const html = await chunkHtml(collection, id);
    const box = document.createElement('div'); box.className = 'chunk-open'; box.innerHTML = html;
    host.after(box);
    btn.setAttribute('aria-expanded', 'true'); if (label) label.textContent = 'Hide text';
  } catch (e) { snack(e.message); }
  btn.disabled = false;
}
async function chunkHtml(collection, id) {
  const enc = encodeURIComponent(collection);
  const [pt, prov] = await Promise.all([api(`/api/v1/collections/${enc}/points/${encodeURIComponent(id)}`), api(`/api/v1/collections/${enc}/provenance/${encodeURIComponent(id)}?text=false`).catch(() => null)]);
  const m = { ...(pt.metadata || {}) }; const text = pt.text || m.text || m.text_content || ''; delete m.text; delete m.text_content;
  const here = collection === state.collection;
  const doc = m._vx_doc && here && can('document.read') ? `<button class="btn sm" ${on('click', ['openDocument', String(m._vx_doc), Number.isInteger(m._vx_start) ? m._vx_start : null, Number.isInteger(m._vx_end) ? m._vx_end : null])}>${icon('book', 14)} Open document</button>` : '';
  const del = here && canWrite() ? `<button class="btn sm danger" ${on('click', ['deletePoint', String(id)])}>Delete</button>` : '';
  const facts = prov ? [['Build', prov.build_id ? { html: idChip(prov.build_id) } : null], ['Document', prov.document_id], ['Version', prov.document_version], ['Source', prov.source], ['Chunking', prov.chunk_strategy ? `${prov.chunk_strategy} ${prov.chunk_size || ''}/${prov.chunk_overlap || ''}` : null], ['Quality', prov.quality]].filter(([, v]) => v !== null && v !== undefined) : [];
  return `${text ? `<div class="chunk-text">${esc(text)}</div>` : '<div class="faint">This chunk has no text stored with it.</div>'}
    <div class="grid two"><div><h3 class="muted" style="margin-bottom:6px">Metadata</h3><pre>${esc(JSON.stringify(m, null, 2))}</pre></div>
    <div><h3 class="muted" style="margin-bottom:6px">Provenance</h3>${facts.length ? `<div class="table">${facts.map(([k, v]) => `<div class="tr t-kv"><span class="muted">${k}</span>${v && v.html ? `<span>${v.html}</span>` : mono(v)}</div>`).join('')}</div>` : '<span class="faint">No provenance stamps: written with a plain add().</span>'}</div></div>
    ${doc || del ? `<div class="row" style="justify-content: flex-end">${doc}${del}</div>` : ''}`;
}
async function deletePoint(id) {
  try { await api(`/api/v1/collections/${encodeURIComponent(state.collection)}/points`, { method: 'DELETE', body: JSON.stringify({ ids: [id] }) }); snack('Deleted'); delete state.health[state.collection]; loadPoints(); } catch (e) { snack(e.message); }
}

/* --------------------------------------------------- how good is a match */
// "relevance" is 0 to 1 and means the same thing in every mode. "score" is
// whatever the mode ranks by, and in hybrid it tops out near 0.018, which
// reads as terrible beside the best result there is. So the relevance leads,
// in words as well as a number, and the score sits under it, small.
const RELEVANCE_WORDS = {
  // Where the words change. Similarities from the bundled models bunch between 0.5 and 0.9,
  // so the bar for "strong" is high. These are for reading a page; a cut-off for answering
  // belongs in your own code, measured on your own questions.
  similarity: { cuts: [0.78, 0.6], says: 'How close this chunk is to your question in meaning, from 0 to 100%. It means the same in every mode and on every engine, so it is the number to set a cut-off on.' },
  reranker: { cuts: [0.6, 0.3], says: 'A second model read your question and this chunk together and judged the pair, from 0 to 100%. It is strict: a chunk on the right topic that does not answer the question scores low.' },
  relative: { cuts: null, says: 'Keyword scores have no ceiling, so this is the score as a share of the best hit. The top hit is always 100%, which says it won, not that it is good.' },
  distance: { cuts: null, says: 'This collection does not use cosine, so this is 1 / (1 + distance): ordered and bounded, and not comparable with a similarity.' },
};
function verdict(r) {
  const raw = `<span class="raw" title="What this mode ranks by. In hybrid it is a sum of reciprocal ranks, where 0.018 is the most any chunk can score, so it orders results and says nothing about how good they are.">score ${Number(r.score || 0).toFixed(4)}</span>`;
  if (typeof r.relevance !== 'number') return `<div class="score"><span class="pct none">–</span><span class="word">no relevance from this engine</span>${raw}</div>`;
  const kind = RELEVANCE_WORDS[r.relevance_kind] || RELEVANCE_WORDS.similarity;
  const level = !kind.cuts ? '' : r.relevance >= kind.cuts[0] ? 'strong' : r.relevance >= kind.cuts[1] ? 'fair' : 'weak';
  const word = r.relevance_kind === 'relative' ? 'of the best hit' : r.relevance_kind === 'distance' ? 'by distance' : `${level} match`;
  const both = r.relevances ? Object.entries(r.relevances).map(([k, v]) => `${k} ${Math.round(v * 100)}%`).join(', ') : '';
  return `<div class="score"><span class="pct ${level}" title="${esc(kind.says)}">${Math.round(r.relevance * 100)}%</span><span class="word">${word}${r.relevance_kind === 'reranker' ? ', reranked' : ''}</span>${both ? `<span class="word" title="Each model's own similarity">${esc(both)}</span>` : ''}${raw}</div>`;
}
function textQuality(q) {
  return `<span class="pill ${q < 0.78 ? 'warn' : ''}" title="How cleanly this chunk's text was extracted from its file, from 0 to 1. Scanned pages and OCR noise lower it; below 0.78 it is worth a look. It is about the text, not about how well it matches your search.">text quality ${Number(q).toFixed(2)}</span>`;
}

/* -------------------------------------------------------------- search */
async function loadSearchPage() {
  if (!state.collections.length) await loadCollectionList();
  fillCollectionSelects();
  $('s-collection').onchange = availableModes;
  await availableModes();
}
// A mode the chosen collection cannot do is greyed out and says why, and if
// it was the one selected the page moves to one that works.
async function availableModes() {
  const name = $('s-collection').value; const c = state.collections.find((x) => x.name === name) || {};
  let h = state.health[name];
  if (name && !h) { try { h = state.health[name] = await api(`/api/v1/collections/${encodeURIComponent(name)}/health`); } catch (e) { h = null; } }
  await loadModels();
  const caps = capsOf(c, h);
  const reranker = (state.models?.models || []).some((m) => String(m.type || '').includes('reranker') && m.present);
  const why = {
    hybrid: caps.hybrid ? '' : 'This collection has no text index, so there are no keywords to fuse with. Dense works.',
    keyword: caps.keyword ? '' : 'This collection has no text index, so there are no keywords to search. Dense works.',
    rerank: reranker ? '' : 'No reranker model is on this machine. Fetch one with: vectrixdb download-models',
    dense: '',
  };
  if (why[state.searchMode]) state.searchMode = 'dense';
  document.querySelectorAll('[data-mode]').forEach((b) => {
    const off = !!why[b.dataset.mode];
    b.disabled = off; b.setAttribute('aria-disabled', String(off));
    if (!b.dataset.says) b.dataset.says = b.title;
    b.title = off ? why[b.dataset.mode] : b.dataset.says;
    b.classList.toggle('on', b.dataset.mode === state.searchMode);
  });
}
function setMode(m) { const chip = document.querySelector(`[data-mode="${m}"]`); if (chip && chip.disabled) return; state.searchMode = m; document.querySelectorAll('[data-mode]').forEach((b) => b.classList.toggle('on', b.dataset.mode === m)); }
function switchTab(tab) {
  ['results'].forEach((t) => { $(`tab-${t}`).classList.toggle('on', t === tab); $(`tabpanel-${t}`).classList.toggle('on', t === tab); });
}
async function runSearch() {
  const collection = $('s-collection').value; const q = $('s-query').value.trim(); const limit = Math.min(50, Math.max(1, parseInt($('s-limit').value) || 10));
  if (!collection) { snack('Pick a collection'); return; }
  if (!q) { $('s-query').focus(); snack('Type a query'); return; }
  const enc = encodeURIComponent(collection); let path; let body = { query_text: q, limit };
  // Results carry an excerpt, cut on the server. The rest of a chunk is asked for, one at a time.
  const excerpt = '?snippet=200';
  if (state.searchMode === 'keyword') path = `/api/v1/collections/${enc}/keyword-search`;
  else if (state.searchMode === 'hybrid') path = `/api/v1/collections/${enc}/text-hybrid-search`;
  else if (state.searchMode === 'rerank') { path = `/api/v1/collections/${enc}/text-search`; body.rerank = true; }
  else path = `/api/v1/collections/${enc}/text-search`;
  const t0 = performance.now();
  $('s-results').innerHTML = '<div class="muted">Searching…</div>';
  try {
    const raw = await fetch(`${at(path)}${excerpt}`, { method: 'POST', headers: headers(true), body: JSON.stringify(body) });
    const ms = performance.now() - t0; state.timings.push(ms);
    const bodyJson = await raw.json();
    // The collection's policy said no: the shape of results, blurred, and the note saying why.
    if (raw.status === 403 && bodyJson.data && bodyJson.data.code) { $('s-results').innerHTML = restrictedResults(collection, bodyJson.data); return; }
    if (!raw.ok) { const d = bodyJson.detail || bodyJson.message || 'Search failed'; $('s-results').innerHTML = `<div class="notice ${raw.status === 403 ? 'warn' : 'bad'}">${icon(raw.status === 403 ? 'lock' : 'x')}<div>${esc(typeof d === 'object' ? JSON.stringify(d) : d)}</div></div>`; return; }
    const data = bodyJson.data || bodyJson; const results = data.results || []; const redacted = !!data._redacted;
    $('s-metrics').innerHTML = [metric('Returned', String(results.length)), metric('Mode', state.searchMode === 'rerank' ? 'rerank' : (data.search_mode || state.searchMode)), metric('Server time', data.query_time_ms !== undefined ? `${Number(data.query_time_ms).toFixed(1)} ms` : null), metric('Round trip', `${Math.round(ms)} ms`)].join('');
    if (!results.length) { $('s-results').innerHTML = `<div class="empty"><h2>No results</h2><div>Nothing in ${esc(collection)} matched. Try hybrid mode, or fewer words.</div></div>`; return; }
    $('s-results').innerHTML = `<div class="card" style="padding-top: 4px">${redacted ? `<div class="notice warn">${icon('lock')}<div>Ids are masked and vectors hidden: this server has a key and none was entered.</div></div>` : ''}${results.map((r, i) => {
      const m = r.metadata || r.payload || {}; let text = r.text || m.text || m.text_content || '';
      // A chunk stored as a vector alone has no text, and its metadata is not a stand-in for one.
      if (!text) text = '<span class="faint">No text is stored with this chunk.</span>';
      else if (r.highlights && r.highlights.length) text = highlight(text, r.highlights);
      else text = esc(text);
      const chips = []; ['dense_score', 'sparse_score', 'keyword_score', 'rerank_score', 'rrf_score'].forEach((k) => { if (typeof r[k] === 'number') chips.push(pill(`${k.replace('_score', '')} ${r[k].toFixed(3)}`, 'info')); });
      if (r.matched_by && r.matched_by.length) chips.unshift(`<span class="pill info" title="Which of the two searches found this chunk">found by ${esc(r.matched_by.join(' + '))}</span>`);
      if (m._vx_quality !== undefined) chips.push(textQuality(m._vx_quality));
      const more = r.snipped && !redacted && can('content.read') ? revealButton(collection, r.id) : '';
      return `<div class="result"><div class="rank">${i + 1}</div><div class="body"><div class="row">${mono(r.id)}${m._vx_citation ? `<span class="tag mono" title="${esc(m._vx_citation)}">${esc(m._vx_readable_citation || `[${m._vx_citation}]`)}</span>` : ''}${m.topic ? tag(m.topic) : ''}</div><div class="text">${text}</div><div class="row" style="justify-content: space-between"><span class="row">${chips.join('')}</span>${more}</div></div>${verdict(r)}</div>`;
    }).join('')}${`<div class="row" style="padding-top: 12px; justify-content: space-between"><span class="faint">${data.total_searched !== undefined ? `${fmtNum(data.total_searched)} searched · ` : ''}${state.searchMode === 'hybrid' ? 'dense and sparse fused with RRF' : state.searchMode === 'rerank' ? 'dense candidates re-ranked by the cross-encoder' : ''}</span><button class="btn sm" data-on-click="[[&quot;copySearchSnippet&quot;]]">${icon('copy', 14)} Copy as Python</button></div>`}</div>`;
  } catch (e) { $('s-results').innerHTML = e.status === 403 && e.body && e.body.data && e.body.data.code ? restrictedResults(collection, e.body.data) : `<div class="notice bad">${icon('x')}<div>${esc(e.message)}</div></div>`; }
}
/* Off the collection's policy: the shape of a results page, blurred, and the notice over it. Nothing real is drawn: the rows are placeholders. */
function restrictedResults(collection, data) {
  const rows = [1, 2, 3].map((i) => `<div class="result"><div class="rank">${i}</div><div class="body"><div class="row">${mono('chunk')}</div><div class="text">A chunk that is not yours to read. Its words are not on this page, only the shape of one, so you can tell there is something here and not what it says.</div></div></div>`).join('');
  const why = {
    no_policy: `${collection} is unavailable to anyone yet.`,
    not_in_token: `You're not in a group ${collection}'s policy names.`,
    not_on_list: data.policy === 'token' ? `You're in one of its groups, but not on ${collection}'s list.` : `You're not on ${collection}'s list.`,
  }[data.code] || `You're not on ${collection}'s policy.`;
  // An admin can change it, so the note takes them there; anybody else is told who can.
  const next = can('collection.share') ? `<a class="btn sm" href="#/collections/${encodeURIComponent(collection)}/policy">Open its Policy tab</a>` : '<span>Its admin can add you.</span>';
  return `<div class="restricted"><div class="restricted-rows" aria-hidden="true">${rows}</div><div class="restricted-veil"><div class="restricted-note" role="alert">${icon('alert', 28)}<b>Restricted</b><span>${esc(why)}</span>${next}</div></div></div>`;
}
function highlight(text, hs) { let out = esc(text); hs.slice(0, 8).forEach((h) => { const frag = esc(typeof h === 'string' ? h : (h.text || h.term || '')); if (frag) out = out.split(frag).join(`<mark>${frag}</mark>`); }); return out; }
function searchSnippet() { const c = $('s-collection').value; const q = $('s-query').value.trim(); const mode = state.searchMode === 'rerank' ? 'ultimate' : state.searchMode === 'keyword' ? 'sparse' : state.searchMode; return `from vectrixdb import Vectrix\n\ndb = Vectrix(${JSON.stringify(c)}, mode=${JSON.stringify(mode === 'sparse' ? 'hybrid' : mode)})\nhits = db.search(${JSON.stringify(q)}, mode=${JSON.stringify(mode)}, explain=True)\nfor hit in hits:\n    print(round(hit.score, 3), hit.text[:80])`; }
function trySearch(q, mode) { go('#/search'); setTimeout(() => { $('s-query').value = q; setMode(mode === 'late' ? 'rerank' : mode); switchTab('results'); runSearch(); }, 80); }

/* --------------------------------------------------------------- graph */
/* -------------------------------------------------------------- ingest */
let ingestFiles = [];
async function loadIngest() {
  if (!state.collections.length) await loadCollectionList();
  fillCollectionSelects();
  $('ing-write').style.display = canWrite() ? '' : 'none'; $('ing-nokey').style.display = canWrite() ? 'none' : '';
  try {
    const r = await api('/api/v1/extractors'); state.readers = r;
    $('file-input').setAttribute('accept', (r.accepted || []).join(','));
    const routed = (r.extractors || []).filter((s) => s !== '*');
    $('ing-readers').textContent = `This server reads ${(r.built_in || []).join(' ')}`
      + (routed.length ? `, and hands ${routed.join(' ')} to an extraction service` : '')
      + `. Images, audio and video need an extra or an extraction service. ${r.keeps_source ? 'It keeps the Markdown of what it indexes.' : 'It does not keep documents: start it with VECTRIXDB_KEEP_SOURCE=1 to open them from a point.'}`;
  } catch (e) { state.readers = null; $('ing-readers').textContent = ''; }
  await renderDocs();
  if (typeof loadPoison === 'function') await loadPoison();
}
/* The document index, ten a page, found by title, id or type. */
async function renderDocs() {
  const pg = pageState('docs');
  try { const d = await api(`/api/v1/documents?${pageQuery('docs')}`); state.docs = d.documents || []; state.docsTotal = d.total || 0; } catch (e) { state.docs = []; state.docsTotal = 0; }
  const rows = state.docs.map((d) => `<div class="tr t-docs"><span>${esc(d.title || d.doc_id)}</span><span>${esc(d.doc_type)}</span>${mono(d.page_count)}${mono(d.section_count)}<span class="muted">${ago(d.indexed_at)}</span><span>${canWrite() ? `<button class="btn sm danger" ${on('click', ['deleteDoc', d.doc_id])}>Delete</button>` : ''}</span></div>`).join('');
  $('doc-list').innerHTML = state.docsTotal || pg.q.trim()
    ? `${pager('docs', { placeholder: 'Find a document', q: pg.q, total: state.docsTotal, from: pg.page * PAGE, count: state.docs.length })}<div class="table"><div class="tr head t-docs"><span>Title</span><span>Type</span><span>Pages</span><span>Sections</span><span>Indexed</span><span></span></div>${rows || `<div class="tr t-one"><span class="muted">No document is called that.</span></div>`}</div>`
    : `<div class="muted">No documents in the document index yet.</div>`;
}
PAGERS.docs = () => renderDocs();
function setupDrop() {
  const z = $('drop'); if (!z) return;
  ['dragenter', 'dragover'].forEach((ev) => z.addEventListener(ev, (e) => { e.preventDefault(); z.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => z.addEventListener(ev, (e) => { e.preventDefault(); z.classList.remove('over'); }));
  z.addEventListener('drop', (e) => addFiles(e.dataTransfer.files));
  $('file-input').addEventListener('change', (e) => addFiles(e.target.files));
}
function addFiles(list) { for (const f of list) ingestFiles.push(f); renderFiles(); }
function renderFiles() { $('file-list').innerHTML = ingestFiles.map((f, i) => `<div class="tr t-files"><span>${esc(f.name)}</span><span class="faint">${fmtBytes(f.size)}</span><button class="btn sm" ${on('click', ['dropFile', i])}>${icon('x', 12)}</button></div>`).join(''); }
function chunkText(text, size) {
  const paras = text.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean); const out = []; let cur = '';
  for (const p of paras) { if ((cur + '\n\n' + p).length > size && cur) { out.push(cur); cur = p; } else cur = cur ? cur + '\n\n' + p : p; }
  if (cur) out.push(cur);
  return out.flatMap((c) => c.length <= size * 1.5 ? [c] : c.match(new RegExp(`[\\s\\S]{1,${size}}`, 'g')));
}
async function runIngest() {
  const target = $('ing-target').value; const collection = $('ing-collection').value; const log = $('ing-log'); log.innerHTML = '';
  const pasted = $('ing-text').value.trim(); const items = [];
  if (pasted) items.push({ name: $('ing-title').value.trim() || 'pasted text', text: pasted });
  if (target === 'server') { await ingestThroughServer(collection, pasted, log); return; }
  for (const f of ingestFiles) { try { items.push({ name: f.name, text: await f.text() }); } catch (e) { log.innerHTML += `<div class="notice bad">${icon('x')}<div>${esc(f.name)}: could not read as text</div></div>`; } }
  if (!items.length) { snack('Paste text or add a file'); return; }
  const size = Math.max(200, parseInt($('ing-chunk').value) || 800); let meta = {};
  try { meta = $('ing-meta').value.trim() ? JSON.parse($('ing-meta').value) : {}; } catch (e) { log.innerHTML = `<div class="notice bad">${icon('x')}<div>Metadata is not valid JSON.</div></div>`; return; }
  ingestRun = []; $('ing-quality').innerHTML = '';
  for (const it of items) {
    try {
      if (target === 'documents') {
        const d = await api('/api/v1/documents', { method: 'POST', body: JSON.stringify({ text: it.text, title: it.name, doc_type: /\.md$/i.test(it.name) ? 'markdown' : 'text', metadata: meta }) });
        log.innerHTML += `<div class="notice">${icon('check')}<div><b>${esc(it.name)}</b> indexed as document ${mono(d.doc_id || d.id || '')}</div></div>`;
      } else {
        if (!collection) { snack('Pick a collection'); return; }
        const chunks = chunkText(it.text, size); const stem = it.name.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_-]+/g, '_');
        const points = chunks.map((c, i) => ({ id: `${stem}#${i}`, text: c, payload: { ...meta, text: c, source: it.name, chunk: i } }));
        for (let i = 0; i < points.length; i += 32) await api(`/api/v1/collections/${encodeURIComponent(collection)}/text-upsert`, { method: 'POST', body: JSON.stringify({ points: points.slice(i, i + 32) }) });
        log.innerHTML += `<div class="notice">${icon('check')}<div><b>${esc(it.name)}</b>: ${countOf(chunks.length, 'chunk')} into ${mono(collection)}</div></div>`;
        delete state.health[collection];
      if (typeof d.quality === 'number') { ingestRun.push({ name: it.name, quality: d.quality }); $('ing-quality').innerHTML = trBatch(ingestRun, d.quality_threshold || 0.78); }
      }
    } catch (e) { log.innerHTML += `<div class="notice bad">${icon('x')}<div><b>${esc(it.name)}</b>: ${esc(e.message)}</div></div>`; }
  }
  ingestFiles = []; renderFiles(); $('ing-text').value = ''; loadIngest();
}
let ingestRun = [];
async function ingestThroughServer(collection, pasted, log) {
  if (!collection) { snack('Pick a collection'); return; }
  let meta = {};
  try { meta = $('ing-meta').value.trim() ? JSON.parse($('ing-meta').value) : {}; } catch (e) { log.innerHTML = `<div class="notice bad">${icon('x')}<div>Metadata is not valid JSON.</div></div>`; return; }
  const items = ingestFiles.map((f) => ({ name: f.name, body: f }));
  if (pasted) items.push({ name: ($('ing-title').value.trim() || 'pasted-text').replace(/[\\/]+/g, '-') + '.md', body: new Blob([pasted], { type: 'text/markdown' }) });
  if (!items.length) { snack('Paste text or add a file'); return; }
  const size = Math.max(200, parseInt($('ing-chunk').value) || 800);
  const query = new URLSearchParams({ chunk: 'markdown', chunk_size: String(size), overlap: String(Math.floor(size / 5)) });
  if (Object.keys(meta).length) query.set('metadata', JSON.stringify(meta));
  for (const it of items) {
    try {
      const d = await api(`/api/v1/collections/${encodeURIComponent(collection)}/documents?${query}`, { method: 'POST', body: it.body, headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(it.name) } });
      const cites = (d.citations || []).slice(0, 5).map((c) => `<span class="tag mono">[${esc(c)}]</span>`).join('') + ((d.citations || []).length > 5 ? `<span class="faint">and ${d.citations.length - 5} more</span>` : '');
      log.innerHTML += `<div class="notice ${d.low_quality ? 'warn' : ''}">${icon(d.low_quality ? 'info' : 'check')}<div><b>${esc(it.name)}</b>: ${countOf(d.chunks, 'chunk')} into ${mono(collection)}${d.replaced ? `, replacing ${fmtNum(d.replaced)}` : ''}. Read by ${esc(d.extractor)}, quality ${Number(d.quality).toFixed(2)}${d.low_quality ? ', which reads as a failed extraction' : ''}${d.pages ? `, ${countOf(d.pages, 'page')}` : ''}${d.figures ? `, ${countOf(d.figures, 'figure')}` : ''}.<div class="row" style="margin-top:6px">${cites}</div></div></div>`;
      delete state.health[collection];
    } catch (e) { log.innerHTML += `<div class="notice bad">${icon('x')}<div><b>${esc(it.name)}</b>: ${esc(e.message)}</div></div>`; }
  }
  ingestFiles = []; renderFiles(); $('ing-text').value = ''; loadIngest();
}
async function openDocument(docId, start, end) {
  const path = String(docId).split('/').map(encodeURIComponent).join('/');
  try {
    const text = await apiText(`/api/v1/collections/${encodeURIComponent(state.collection)}/documents/${path}`);
    const a = Number.isInteger(start) ? Math.max(0, Math.min(start, text.length)) : null; const b = Number.isInteger(end) ? Math.max(a || 0, Math.min(end, text.length)) : null;
    $('doc-title').textContent = docId;
    $('doc-note').textContent = a === null ? 'The Markdown this document was indexed from.' : `The Markdown this document was indexed from, with this chunk marked: characters ${a} to ${b} of ${text.length}.`;
    $('doc-view').innerHTML = a === null ? esc(text) : `${esc(text.slice(0, a))}<mark id="doc-mark">${esc(text.slice(a, b))}</mark>${esc(text.slice(b))}`;
    openDialog('dlg-doc'); const mark = $('doc-mark'); if (mark) mark.scrollIntoView({ block: 'center' });
  } catch (e) { snack(e.message); }
}
async function deleteDoc(id) { try { await api(`/api/v1/documents/${encodeURIComponent(id)}`, { method: 'DELETE' }); snack('Deleted'); renderDocs(); } catch (e) { snack(e.message); } }

/* --------------------------------------------------------------- audit */
async function loadAudit() {
  const el = $('audit-body');
  try {
    const d = await api(`/api/v1/audit?${pageQuery('audit')}`);
    if (!d.available) { el.innerHTML = `<div class="notice info">${icon('info')}<div><b>No audit trail is configured for this server.</b> ${esc(d.reason)}. The library writes records through an AuditSink; point the server at a JSONL sink to read them here:</div></div><pre>VECTRIXDB_AUDIT_JSONL=/var/log/vectrixdb/audit.jsonl vectrixdb serve --api-key ...</pre><pre>from vectrixdb import Vectrix
from vectrixdb.audit import JSONLSink, DENY

sink = JSONLSink("/var/log/vectrixdb/audit.jsonl", query_key=KEY, on_failure=DENY)
db = Vectrix("lending_memos", policy=policy, audit=sink)</pre>`; return; }
    const c = d.counts; const recs = d.records;
    el.innerHTML = `<div class="grid metrics">${metric('Decisions', fmtNum(c.decisions))}${metric('Denied', fmtNum(c.denied))}${metric('Undecidable', fmtNum(c.undecidable), c.undecidable ? 'page somebody' : '', c.undecidable ? 'bad' : '')}${metric('Refused', fmtNum(c.refused), '', c.refused ? 'bad' : '')}${metric('Ingestions', fmtNum(c.ingestions))}</div>
      <div class="grid lay-trends" id="au-trends"${d.daily ? '' : ' hidden'}></div>
      <div id="audit-records">${auditRecordsHtml(d)}</div>`;
    if (d.daily) trAudit($('au-trends'), d.daily, d.by_collection || []);
  } catch (e) {
    el.innerHTML = `<div class="notice ${e.status === 403 ? 'warn' : 'bad'}">${icon('lock')}<div><b>${esc(e.message)}.</b> The audit log is more sensitive than the index it audits: it records which restricted things exist and who was refused them. ${state.authEnabled && !state.apiKey ? 'Enter the API key to read it.' : 'Start the server with --api-key and set VECTRIXDB_AUDIT_JSONL to read it here.'}</div></div>`;
  }
}

/* The records card: newest first, ten a page, found by collection, person, outcome or id. */
function auditRecordsHtml(d) {
  const pg = pageState('audit'); const recs = d.records || [];
  const rows = recs.map((r) => { const id = r.decision_id || r.ingestion_id; const ing = !!r.ingestion_id; const o = String(r.outcome || ''); const kind = ing ? 'info' : o.startsWith('allowed') ? 'ok' : o.startsWith('denied') ? 'warn' : o ? 'bad' : ''; const label = ing ? `ingestion, ${fmtNum(r.documents_written || 0)} written` : (o || 'decision') + (r.withheld_disclosable ? `, ${r.withheld_disclosable} withheld` : ''); return `<div class="tr t-audit"><span>${idChip(id)}</span>${mono(r.collection || '')}<span>${esc(r.principal_id || (ing ? 'service' : '–'))}</span><span>${pill(label, kind)}</span><span>${idChip(r.index_build_id)}</span><span class="faint">${r.padded_to_ms ? Math.round(r.padded_to_ms) + ' ms' : r.duration_ms ? Math.round(r.duration_ms) + ' ms' : '–'}</span>${mono(short(r.query_fingerprint || '–', 4), 'faint')}</div>`; }).join('');
  return `<div class="card"><div class="head"><h2>Records, newest first</h2><span class="faint mono">${esc(d.path)}</span></div>
      ${pager('audit', { placeholder: 'Find by collection, person or outcome', q: pg.q, total: d.total || 0, from: d.offset || 0, count: recs.length })}
      <div class="table"><div class="tr head t-audit"><span>Record</span><span>Collection</span><span>Principal</span><span>Outcome</span><span>Build</span><span>Time</span><span>Query</span></div>
      ${rows || `<div class="tr t-one"><span class="muted">${pg.q.trim() ? 'No record says that.' : 'No records yet.'}</span></div>`}</div>
      <div class="faint">Query text is never stored, only its keyed fingerprint. Withheld from this page: ${(d.withheld || []).join(', ')}.</div></div>`;
}
PAGERS.audit = async () => { const holder = $('audit-records'); if (!holder) return; try { const d = await api(`/api/v1/audit?${pageQuery('audit')}`); holder.innerHTML = auditRecordsHtml(d); } catch (e) { snack(e.message); } };

/* ------------------------------------------------------------- console */
function presetConsole(kind, collection) {
  const c = collection || $('s-collection')?.value || state.collections[0]?.name || 'your-collection';
  const presets = {
    info: ['GET', '/api/v1/info', ''],
    list: ['GET', '/api/v1/collections', ''],
    search: ['POST', `/api/v1/collections/${c}/text-hybrid-search`, JSON.stringify({ query_text: 'how does sleep affect memory', limit: 5 }, null, 2)],
    upsert: ['POST', `/api/v1/collections/${c}/text-upsert`, JSON.stringify({ points: [{ id: 'note-1', text: 'Aerobic exercise raises BDNF and improves executive function.', payload: { topic: 'neuroscience' } }] }, null, 2)],
    health: ['GET', `/api/v1/collections/${c}/health`, ''],
    quality: ['GET', `/api/v1/collections/${c}/quality`, ''],
    models: ['GET', '/api/v1/models', ''],
  };
  const [m, p, b] = presets[kind] || presets.info; $('con-method').value = m; $('con-path').value = p; $('con-body').value = b;
}
async function runConsole() {
  const method = $('con-method').value; const path = $('con-path').value.trim(); const body = $('con-body').value.trim();
  if (!path.startsWith('/')) { snack('The path starts with /'); return; }
  const t0 = performance.now(); const opts = { method, headers: headers(!!body) }; if (method !== 'GET' && body) opts.body = body;
  try { const res = await fetch(at(path), opts); const ms = Math.round(performance.now() - t0); let data; try { data = await res.json(); } catch (e) { data = await res.text(); }
    $('con-status').innerHTML = pill(`${res.status} · ${ms} ms`, res.ok ? 'ok' : 'bad'); $('con-out').textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
  } catch (e) { $('con-status').innerHTML = pill('failed', 'bad'); $('con-out').textContent = e.message; }
  $('con-curl').textContent = `curl -X ${method} ${at(path)}${state.apiKey ? ` -H "${KEY_HEADER}: $VECTRIXDB_API_KEY"` : ''}${body && method !== 'GET' ? ` -H "Content-Type: application/json" -d '${body.replace(/\n\s*/g, ' ')}'` : ''}`;
}

// The code only: the block's "Copy" label is not part of it.
function copyCode(el) { const pre = el.querySelector('pre'); copyText((pre || el).innerText); }
function copyText(t) { navigator.clipboard.writeText(t).then(() => snack('Copied')).catch(() => snack('Could not copy')); }

/* ------------------------------------------------------------- sign-in */
/* The first question the page asks is who is here. With sign-in off the
   answer is "nobody needs to be" and the page carries on as it always has.
   With it on and nobody signed in, the application is not shown at all,
   unless the server lets guests browse: then they see what is shared, and
   the Sign in button at the top right is how they get the rest. */
const SIGNIN_ERRORS = {
  no_role: () => `Your company sign-in worked, but only members of the security group set up for ${BRAND.name} can use it.`,
  not_listed: () => `Your company sign-in worked, but only the addresses on the list for ${BRAND.name} can use it.`,
  turned_off: () => `Your company sign-in worked, but your access to ${BRAND.name} is turned off.`,
  groups_overflow: () => 'You are in more groups than fit in a sign-in token, and this server has not been told where to look them up. Ask whoever runs it.',
  provider_unreachable: () => 'Your company sign-in could not be reached. Try again in a moment.',
  expired: () => 'That sign-in took too long, or was started in another window. Try again.',
  state: () => 'That sign-in was started somewhere else. Try again from here.',
  // What the company's sign-in page sent back instead of a sign-in. Only these are put in words; the address can say anything.
  provider_access_denied: () => "Your company's sign-in page didn't let it through, or it was cancelled there. Try again, or ask your IT team if you expected to get in.",
  provider_temporarily_unavailable: () => 'Your company sign-in is busy or down for a moment. Try again shortly.',
  provider_server_error: () => 'Your company sign-in had a fault of its own. Try again shortly.',
};
const NO_ACCESS = ['no_role', 'not_listed', 'turned_off'];
const DOTS = '<span class="dots" aria-hidden="true"><i>.</i><i>.</i><i>.</i></span>';
const METHOD_WORDS = { oidc: 'Signed in with SSO', email: 'Signed in with email and code', passkey: 'Signed in with a passkey', break_glass: 'Signed in with emergency sign-in', developer: 'Signed in with Developer Access' };

// Back from single sign-on: the page painted a spinner before the script loaded. It goes now, and so does the marker in the address.
function bootDone() {
  const boot = $('vx-boot'); if (boot) boot.remove();
  if (/[?&]sso=1\b/.test(location.search)) history.replaceState(null, '', location.pathname + location.hash);
}
async function whoAmI() {
  const hash = location.hash;
  if (hash.startsWith('#/break-glass')) { bootDone(); await gateBreakGlass(); return false; }
  if (hash.startsWith('#/enrol') || hash.startsWith('#/password')) {
    // Asked first, so a server that takes passkeys only draws only the passkey.
    try { await api('/auth/me', { quiet: true }); } catch (e) { if (e.body && e.body.data && e.body.data.methods) state.methods = e.body.data.methods; }
    state.methods = state.methods || { email: {} }; state.signinOn = true; bootDone(); showGate(); return false;
  }
  try {
    const d = await api('/auth/me', { quiet: true });
    state.signinOn = !!d.signin;
    state.me = d.signin ? { ...d.person, actions: d.actions, seesContent: d.sees_content, people: d.people, admins: d.admins, passwords: d.passwords, passkeys: d.passkeys, ssoPending: !!d.sso_pending, requirePasskey: !!d.require_passkey, breakGlass: d.break_glass || null, ownWays: d.own_ways !== false } : null;
    state.version = d.version || state.version; state.guestsOn = !!d.guests;
    if (d.must_add_passkey) { bootDone(); gateMustAddPasskey(); return false; }
  } catch (e) {
    if (e.status === 401 && e.body && e.body.data) {
      state.signinOn = true; state.methods = e.body.data.methods || {}; state.guestsOn = !!e.body.data.guests;
      if (!state.guestsOn) { bootDone(); showGate(); return false; }
      state.guest = true; state.me = null;
    } else state.me = null;
  }
  bootDone();
  state.gated = false;
  $('gate').hidden = true; document.querySelector('.app').hidden = false;
  applyRole();
  if (state.guest && hash.startsWith('#/signin')) showGate();
  return true;
}
function applyRole() {
  const quality = $('ctab-quality'); if (quality) quality.hidden = !can('content.read');
  document.querySelectorAll('.nav a').forEach((a) => { const need = NEEDS[a.dataset.page]; a.hidden = a.dataset.page === 'access' ? !(state.me && can('access.read')) : !!(need && !can(need)); });
  const manage = ['ingest', 'audit', 'access', 'console'].some((p) => !document.querySelector(`.nav a[data-page="${p}"]`).hidden);
  $('nav-manage').hidden = !manage;
  const graph = $('tab-graph'); if (graph) graph.hidden = !can('content.read');
  $('pill-guest').hidden = !state.guest; $('guest-note').hidden = !state.guest; $('pill-backend').hidden = state.guest;
  $('auth-foot').hidden = state.signinOn;
  // The line is for everybody. The version is under About, for admins.
  $('copyline').textContent = COPY;
  renderGlassBanner();
  $('btn-signin').hidden = !state.guest; $('account-wrap').hidden = state.guest;
  renderAccount();
}
function initials(label) { const parts = String(label).replace(/@.*/, '').split(/[\s._-]+/).filter(Boolean); return ((parts[0] || '?')[0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase(); }
function renderAccount() {
  const me = state.me;
  const label = me ? (me.name || me.email || me.subject) : 'This server';
  $('account-face').innerHTML = me ? esc(initials(label)) : icon('settings', 16);
  $('account-name').textContent = label;
  $('account-role').textContent = me ? me.role.charAt(0).toUpperCase() + me.role.slice(1) : 'Settings';
  $('menu-id').hidden = !me; $('menu-id-sep').hidden = !me;
  $('menu-id').innerHTML = me ? `<div class="menu-id"><span class="face">${esc(initials(label))}</span><span class="id-text"><b>${esc(label)}</b>${me.email && me.email !== label ? `<small>${esc(me.email)}</small>` : ''}</span></div><div class="menu-id-sub">${pill(me.role.charAt(0).toUpperCase() + me.role.slice(1), 'acc')}<span>${esc(METHOD_WORDS[me.method] || 'Signed in')}</span></div>` : '';
  $('mi-access').hidden = !(me && can('access.read'));
  $('mi-keys').hidden = !(me && can('keys.manage'));
  $('mi-ways').hidden = !me;
  $('mi-sep').hidden = $('mi-access').hidden && $('mi-keys').hidden && $('mi-ways').hidden;
  $('btn-key').hidden = state.signinOn;
  $('mi-about').hidden = state.signinOn ? !(me && can('about.read')) : false;
  $('mi-about-v').textContent = `VectrixDB${state.version ? ` ${state.version}` : ''}`;
  $('so-sep').hidden = !me; $('mi-signout').hidden = !me;
}
/* Emergency sign-in is on: every page says so to an admin, with when it turns itself off. */
function glassTime(iso) {
  const when = new Date(iso); if (Number.isNaN(when.getTime())) return String(iso);
  const hm = when.toISOString().slice(11, 16);
  const month = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][when.getUTCMonth()];
  return when.toISOString().slice(0, 10) === new Date().toISOString().slice(0, 10) ? `${hm} UTC` : `${when.getUTCDate()} ${month}, ${hm} UTC`;
}
function renderGlassBanner() {
  const glass = state.me && state.me.breakGlass;
  $('glass-banner').hidden = !glass;
  $('glass-banner').innerHTML = glass ? `${icon('alert', 16)}<span><b>Emergency sign-in is on until ${esc(glassTime(glass.until))}.</b> Turn it off when sign-in is back.</span>` : '';
}
/* Which VectrixDB this is, under what licence, and the notice that travels with it. */
async function openAbout() {
  closeAccount();
  $('about-body').innerHTML = '<div class="muted">Loading…</div>'; openDialog('dlg-about');
  try {
    const d = await api('/api/v1/about');
    $('about-body').innerHTML = `<div class="head"><h2>About</h2><button class="btn icon" ${on('click', ['closeDialog', 'dlg-about'])} aria-label="Close">${icon('x', 16)}</button></div>
      <div class="stack" style="gap: 4px"><b class="about-name">${esc(d.name)} <span class="mono">${esc(d.version)}</span></b><span class="muted">${esc(d.licence_line)}</span></div>
      <div class="stack"><span class="faint">Notice</span><pre class="about-notice" tabindex="0">${esc(d.notice)}</pre></div>
      <div class="row end"><a class="btn" href="${esc(at('/api/v1/about/licence'))}" target="_blank" rel="noopener">Read the licence</a><button class="btn primary" ${on('click', ['closeDialog', 'dlg-about'])}>Close</button></div>`;
  } catch (e) { $('about-body').innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(e.message)}</div></div>`; }
}
async function signOut() {
  closeAccount();
  try { await api('/auth/signout', { method: 'POST' }); } catch (e) { /* signed out either way */ }
  state.me = null; location.hash = '#/overview'; location.reload();
}
const SKELETON = '<div class="skeleton" aria-hidden="true"><div class="sk-side"><div class="sk-bar" style="width: 70%"></div><div class="sk-bar" style="width: 90%"></div><div class="sk-bar" style="width: 60%"></div><div class="sk-bar" style="width: 75%"></div></div><div class="sk-main"><div class="sk-top"></div><div class="sk-body"><div class="sk-row"><div class="sk-card"></div><div class="sk-card"></div><div class="sk-card"></div></div><div class="sk-row"><div class="sk-card tall"></div><div class="sk-card tall"></div><div class="sk-card tall"></div></div></div></div></div>';
function gateBox(inner, opts = {}) {
  state.gated = true;
  // A guest keeps the page behind the dialog. Anybody else has no page yet: it is not fetched until they are signed in.
  const alone = !state.guest || !!opts.plain;
  if (alone) document.querySelector('.app').hidden = true;
  const gate = $('gate'); gate.hidden = false; gate.classList.toggle('alone', alone);
  const close = state.guest && !opts.plain ? `<button class="btn icon" data-on-click="[[&quot;closeGate&quot;]]" aria-label="Close">${icon('x', 16)}</button>` : '';
  const step = opts.step ? `<span class="step">${esc(opts.step)}</span>` : (opts.badge || '');
  gate.innerHTML = `${alone && !opts.plain ? SKELETON + '<div class="veil"></div>' : ''}<div class="box${opts.wide ? ' wide' : ''}" role="dialog" aria-modal="true" aria-label="${esc(opts.label || 'Sign in')}"><div class="brand">${brandMark()}<b class="grow${BRAND.wordmark ? ' wordmark' : ''}">${esc(BRAND.name)}</b>${step}${close}</div>${inner}<p class="copyline">${esc(COPY)}</p></div>`;
  const first = gate.querySelector('input'); if (first) setTimeout(() => first.focus(), 30);
}
function closeGate() {
  $('gate').hidden = true; state.gated = false;
  if (/^#\/(signin|enrol|password|break-glass|developer)/.test(location.hash)) history.replaceState(null, '', '#/overview');
}
function showGate() {
  const hash = location.hash;
  if (hash.startsWith('#/enrol')) { const token = new URLSearchParams(hash.split('?')[1] || '').get('token'); return gateEnrol(token); }
  if (hash.startsWith('#/password')) { const token = new URLSearchParams(hash.split('?')[1] || '').get('token'); return gatePassword(token); }
  const error = hash.startsWith('#/signin') ? new URLSearchParams(hash.split('?')[1] || '').get('error') : null;
  if (error) return ssoError(error);
  const m = state.methods || {};
  state.back = hash && !/^#\/(signin|enrol|password|break-glass|developer)/.test(hash) ? hash : (state.back || '#/overview');
  // On this machine, with no single sign-on: say so, then open Developer Access.
  // Asked for and not set up yet: the button is pressed as ever, the spinner checks and says so, and the email way opens.
  const pending = !!(m.oidc && m.oidc.pending);
  // Single sign-on alone: the email way is drawn only once the spinner has opened it.
  const closed = pending && m.email && m.email.stands_in && !state.pastSsoCheck;
  // Developer Access and nothing else: there is no button to press, so the spinner starts by itself.
  if (m.developer && !m.oidc && !m.email && !state.pastDeveloper) return localDetected();
  const sso = m.oidc ? `<button class="btn${pending && state.pastSsoCheck ? '' : ' primary'}" ${on('click', ['pressSso'])}>${icon('log-in', 16)} ${esc(m.oidc.label)}</button>` : '';
  const passkey = m.email && m.email.passkeys !== false && window.PublicKeyCredential ? `<button class="btn${m.oidc ? '' : ' primary'}" data-on-click="[[&quot;signInWithPasskey&quot;]]">${icon('key-round', 16)} Sign in with a passkey</button>` : '';
  let email = m.email && !closed ? `<form data-on-submit="[[&quot;gateEmail&quot;,{&quot;$&quot;:&quot;event&quot;}]]"><label class="field">Work email<input type="email" id="gate-email" autocomplete="username" required placeholder="name@company.com"></label><button class="btn${pending && state.pastSsoCheck ? ' primary' : ''}" type="submit">Continue with email</button></form>` : '';
  const only = m.email && m.email.require === 'passkey';
  if (only && passkey) email = `<button class="linkish" style="align-self: center" data-on-click="[[&quot;showCodeWay&quot;,{&quot;$&quot;:&quot;this&quot;}]]">I don't have a passkey yet</button><div id="gate-code-way" hidden>${email}</div>`;
  if (only && !passkey) email = `<div class="notice warn">${icon('alert')}<div>This browser can't use passkeys, which is how this server signs people in. Open it in Chrome, Edge, Safari or Firefox on a computer with a PIN. If you have no passkey yet, your code gets you as far as making one.</div></div>${email}`;
  const foot = closed ? "Your work account signs you in. There's no password here."
    : pending && !passkey ? "There's no password here. Your authenticator app signs you in."
    : only ? 'This server signs people in with passkeys: the PIN you unlock your computer with.'
    : m.oidc && m.email && !m.email.passwords ? 'Your work account signs you in. An authenticator app you added works too.'
    : passkey ? 'A passkey is the quickest way in: just the PIN you unlock your computer with.'
    : m.email && m.email.passwords ? (m.oidc ? 'Your company account, or your password and authenticator app together, sign you in.' : 'Your password and your authenticator app sign you in together.')
    : m.oidc ? "Your work account signs you in. There's no password here."
    : "There's no password here. Your authenticator app signs you in.";
  gateBox(`<div class="stack"><h1>Sign in</h1><p>You'll see everything your role allows, and you'll come back to this page.</p></div>
    ${sso}${passkey}${(sso || passkey) && email && !only ? '<div class="or">or</div>' : ''}${email}
    <div id="gate-error"></div>
    <div class="foot-note">${esc(foot)}</div>
    ${state.guest ? '<button class="linkish" style="align-self: center" data-on-click="[[&quot;closeGate&quot;]]">Keep browsing as a guest</button>' : ''}`);
}

/* Developer Access: accounts named in this machine's settings and one password,
   for trying the roles here. The server offers it only to a caller on this
   machine, and nothing in the sign-in box names it: the single sign-on button
   is pressed as ever, the spinner finds this is a local machine, and opens it. */
function pressSso() {
  const m = state.methods || {};
  if (m.developer) return localDetected();
  return m.oidc && m.oidc.pending ? checkSso() : startSso();
}
function localDetected() {
  bootDone();
  const m = state.methods || {};
  ssoScreen(`<div class="spin" aria-hidden="true"></div><h1>Local development detected${DOTS}</h1><p aria-live="polite">Preparing developer sign-in options for this machine.</p>`);
  clearTimeout(state.ssoTimer);
  state.ssoTimer = setTimeout(m.oidc && !m.oidc.pending ? localAccess : ssoNotSetUp, 900);
}
function ssoNotSetUp() {
  ssoScreen(`<div class="spin" aria-hidden="true"></div><h1>Single sign-on not configured${DOTS}</h1><p aria-live="polite">Single sign-on is unavailable for this local environment right now. Developer Access is available on localhost.</p><button class="back" ${on('click', ['gateDeveloper'])}>Open it now</button>`);
  clearTimeout(state.ssoTimer);
  state.ssoTimer = setTimeout(gateDeveloper, 1100);
}
/* On this machine with single sign-on set up: the real way in, or Developer Access. */
function localAccess() {
  clearTimeout(state.ssoTimer); $('sso').hidden = true;
  const m = state.methods || {};
  gateBox(`<div class="stack"><h1>Local development detected</h1><p>You are running ${esc(BRAND.name)} on localhost. Choose single sign-on for the real sign-in, or use Developer Access for local role testing on this machine.</p></div>
    <button class="btn primary" ${on('click', ['startSso'])}>${icon('log-in', 16)} ${esc(m.oidc.label)}</button>
    <button class="btn" ${on('click', ['gateDeveloper'])}>${icon('monitor', 16)} Developer Access</button>
    <div class="foot-note">Single sign-on remains the normal sign-in path. Developer Access is limited to localhost and is not exposed in shared or deployed environments.</div>
    <button class="linkish" style="align-self: center" ${on('click', ['leaveDeveloper'])}>Back to sign in</button>`,
  { plain: true, label: 'Local development', badge: `<span class="pill acc">${icon('monitor', 12)}This machine only</span>` });
}
function gateDeveloper() {
  clearTimeout(state.ssoTimer); $('sso').hidden = true;
  const m = state.methods || {};
  gateBox(`<div class="stack"><h1>Developer Access</h1><p>Local role testing is available only on this machine. Use the developer credentials in its settings.</p></div>
    <form ${on('submit', ['developerSignIn', EVENT])}>
      <label class="field">Username<input id="dev-user" autocomplete="username" required placeholder="admin.user" autocapitalize="none" spellcheck="false"></label>
      ${passwordField('Password', 'current-password', "From this machine's settings")}
      <div id="gate-error"></div><button class="btn primary" type="submit">Sign in</button></form>
    ${m.oidc || m.email ? `<button class="linkish" style="align-self: center" ${on('click', ['leaveDeveloper'])}>Back to sign in</button>` : ''}`,
  { plain: true, label: 'Developer Access', badge: `<span class="pill acc">${icon('monitor', 12)}This machine only</span>` });
}
async function developerSignIn(ev) {
  ev.preventDefault();
  try {
    await api('/auth/developer', { method: 'POST', body: JSON.stringify({ username: $('dev-user').value.trim(), password: $('gate-password').value }), quiet: true });
    location.hash = state.back || '#/overview'; location.reload();
  } catch (e) { gateNotice(e.message); $('gate-password').value = ''; $('gate-password').focus(); }
  return false;
}
function leaveDeveloper() { state.pastDeveloper = true; history.replaceState(null, '', location.pathname + '#/signin'); showGate(); }

/* Emergency sign-in, for while the usual sign-in is down. Its address is fixed
   and in the docs, and nothing links to it. Off, the server says there is no
   such thing, and this says so in words. */
async function gateBreakGlass() {
  let until = null;
  try { until = (await api('/auth/break-glass', { quiet: true })).until; } catch (e) { until = null; }
  if (!until) {
    gateBox(`<div class="glass-lock" aria-hidden="true">${icon('lock', 20)}</div>
      <div class="stack"><h1>Emergency sign-in is off</h1><p>Use the usual sign-in. An operator turns this on only while the usual sign-in is down.</p></div>
      <button class="btn primary" ${on('click', ['leaveBreakGlass'])}>Go to sign in</button>`, { plain: true, label: 'Emergency sign-in' });
    return;
  }
  gateBox(`<div class="stack"><h1>Emergency sign-in</h1><p>For when the usual sign-in is down. Every sign-in here is recorded.</p></div>
    <form ${on('submit', ['breakGlassSignIn', EVENT])}>
      <label class="field">Username<input id="glass-user" autocomplete="username" required placeholder="The emergency admin" autocapitalize="none" spellcheck="false"></label>
      <label class="field">Password<input type="password" id="glass-password" autocomplete="current-password" required placeholder="From the key vault"></label>
      <div id="gate-error"></div><button class="btn primary" type="submit">Sign in</button></form>
    <button class="linkish" style="align-self: center" ${on('click', ['leaveBreakGlass'])}>Use the usual sign-in</button>`,
  { plain: true, label: 'Emergency sign-in', badge: `<span class="pill warn">${icon('clock', 12)}On until ${esc(glassTime(until))}</span>` });
}
async function breakGlassSignIn(ev) {
  ev.preventDefault();
  const body = { username: $('glass-user').value.trim(), password: $('glass-password').value };
  try {
    await api('/auth/break-glass', { method: 'POST', body: JSON.stringify(body), quiet: true });
    location.hash = '#/overview'; location.reload();
  } catch (e) { gateNotice(e.message); $('glass-password').value = ''; $('glass-password').focus(); }
  return false;
}
function leaveBreakGlass() { location.hash = '#/signin'; location.reload(); }

/* The moments between here and the company's sign-in page. */
function ssoScreen(inner) { const s = $('sso'); s.innerHTML = `${inner}<p class="copyline">${COPY}</p>`; s.hidden = false; }
function startSso(prompt) {
  // Back to this page, as the browser reaches it: through the gateway, when there is one.
  const to = location.pathname.replace(/index\.html$/, '') + '?sso=1' + (state.back || '#/overview');
  const url = `${at('/auth/oidc/start')}?to=${encodeURIComponent(to)}${prompt ? `&prompt=${encodeURIComponent(prompt)}` : ''}`;
  ssoScreen(`<div class="spin" aria-hidden="true"></div><h1 aria-live="polite">Redirecting to SSO${DOTS}</h1><p>Opening your company's sign-in page for ${esc(BRAND.name)}.</p><button class="back" data-on-click="[[&quot;leaveSso&quot;]]">Back to sign in</button>`);
  clearTimeout(state.ssoTimer);
  state.ssoTimer = setTimeout(() => { location.href = url; }, 350);
}
/* Single sign-on asked for and not set up yet. The button is pressed as ever: the
   spinner checks, says so, and opens the email way, which is how people get in
   until the provider is named. */
function checkSso() {
  ssoScreen(`<div class="spin" aria-hidden="true"></div><h1 aria-live="polite">Checking single sign-on${DOTS}</h1><p>Looking for your company's sign-in page for ${esc(BRAND.name)}.</p><button class="back" ${on('click', ['leaveSso'])}>Back to sign in</button>`);
  clearTimeout(state.ssoTimer);
  state.ssoTimer = setTimeout(() => {
    ssoScreen(`<div class="spin" aria-hidden="true"></div><h1>Single sign-on not configured${DOTS}</h1><p aria-live="polite">Opening email sign-in.</p><button class="back" ${on('click', ['openEmailWay'])}>Open it now</button>`);
    state.ssoTimer = setTimeout(openEmailWay, 1200);
  }, 900);
}
function openEmailWay() {
  clearTimeout(state.ssoTimer); $('sso').hidden = true; state.pastSsoCheck = true;
  history.replaceState(null, '', location.pathname + '#/signin'); showGate();
  const field = $('gate-email'); if (field) field.focus();
}
function leaveSso() { clearTimeout(state.ssoTimer); $('sso').hidden = true; history.replaceState(null, '', location.pathname + '#/signin'); showGate(); }
function ssoError(code) {
  bootDone();
  const other = String(code || '').startsWith('provider_') ? "Your company's sign-in page stopped it." : 'Nothing was changed.';
  const say = (SIGNIN_ERRORS[code] || (() => `${other} Start it again from here, and if it stops again, tell whoever runs ${BRAND.name}.`))();
  if (NO_ACCESS.includes(code)) {
    ssoScreen(`<div class="ring" aria-hidden="true">${icon('user-x', 22)}</div><h1>This account doesn't have access</h1><p>${esc(say)} Ask whoever runs it to add you, or try another account.</p><div class="actions"><button class="btn primary" data-on-click="[[&quot;startSso&quot;,&quot;select_account&quot;]]">Try another account</button><button class="back" data-on-click="[[&quot;leaveSso&quot;]]">Back to sign in</button></div>`);
  } else {
    ssoScreen(`<div class="ring" aria-hidden="true">${icon('alert', 22)}</div><h1>The sign-in didn't finish</h1><p>${esc(say)}</p><div class="actions"><button class="btn primary" data-on-click="[[&quot;startSso&quot;]]">Try again</button><button class="back" data-on-click="[[&quot;leaveSso&quot;]]">Back to sign in</button></div>`);
  }
}

/* Passkeys. The browser speaks ArrayBuffers and the server base64url. */
const b64u = {
  enc: (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''),
  dec: (s) => Uint8Array.from(atob(String(s).replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((String(s).length + 3) % 4)), (c) => c.charCodeAt(0)).buffer,
};
function creationOptions(o) { return { ...o, challenge: b64u.dec(o.challenge), user: { ...o.user, id: b64u.dec(o.user.id) }, excludeCredentials: (o.excludeCredentials || []).map((c) => ({ ...c, id: b64u.dec(c.id) })) }; }
function requestOptions(o) { return { ...o, challenge: b64u.dec(o.challenge), allowCredentials: (o.allowCredentials || []).map((c) => ({ ...c, id: b64u.dec(c.id) })) }; }
function credentialJson(c) {
  const r = c.response;
  return {
    id: c.id, rawId: b64u.enc(c.rawId), type: c.type,
    response: {
      clientDataJSON: b64u.enc(r.clientDataJSON),
      attestationObject: r.attestationObject ? b64u.enc(r.attestationObject) : undefined,
      authenticatorData: r.authenticatorData ? b64u.enc(r.authenticatorData) : undefined,
      signature: r.signature ? b64u.enc(r.signature) : undefined,
      userHandle: r.userHandle ? b64u.enc(r.userHandle) : undefined,
      transports: r.getTransports ? r.getTransports() : [],
    },
  };
}
function gateNotice(message, sso) {
  const el = $('gate-error'); if (!el) return;
  // A server that asks for single sign-on every so often says so, and the way through is one press away.
  const again = sso && state.methods && state.methods.oidc && !state.methods.oidc.pending ? `<button class="btn primary" style="margin-top: 10px" data-on-click="[[&quot;startSso&quot;]]">${icon('log-in', 16)} ${esc(state.methods.oidc.label)}</button>` : '';
  el.innerHTML = `<div class="notice ${sso ? 'warn' : 'bad'}">${icon(sso ? 'alert' : 'x')}<div>${esc(message)}${again}</div></div>`;
}
const needsSso = (e) => !!(e && e.body && e.body.data && e.body.data.sso);
async function signInWithPasskey() {
  try {
    const o = await api('/auth/passkey/begin', { method: 'POST', quiet: true });
    const c = await navigator.credentials.get({ publicKey: requestOptions(o.options) });
    await api('/auth/passkey/finish', { method: 'POST', body: JSON.stringify({ credential: credentialJson(c) }), quiet: true });
    location.hash = state.back || '#/overview'; location.reload();
  } catch (e) { if (e.name !== 'NotAllowedError' && e.name !== 'AbortError') gateNotice(e.message, needsSso(e)); }
}

/* The email way in: the code, and on a server with passwords on, the password with it. */
function passwordField(label, autocomplete, placeholder = '') {
  return `<label class="field">${esc(label)}<span class="pw"><input type="password" id="gate-password" autocomplete="${autocomplete}" required ${autocomplete === 'new-password' ? 'minlength="12"' : ''}${placeholder ? ` placeholder="${esc(placeholder)}"` : ''}><button type="button" class="eye" aria-label="Show the password" data-on-click="[[&quot;togglePw&quot;]]">${icon('eye', 16)}</button></span></label>`;
}
function togglePw() { const f = $('gate-password'); if (f) f.type = f.type === 'password' ? 'text' : 'password'; }
async function gateEmail(ev) {
  ev.preventDefault();
  const email = $('gate-email').value.trim();
  const m = state.methods || {};
  try { await api('/auth/email/begin', { method: 'POST', body: JSON.stringify({ email }), quiet: true }); } catch (e) { /* the page says the same thing either way */ }
  if (m.email && m.email.passwords) {
    gateBox(`<h1>Sign in</h1>
      <div class="chipmail">${icon('mail', 16)}<span>${esc(email)}</span><button class="linkish" data-on-click="[[&quot;showGate&quot;]]">Change</button></div>
      <form ${on('submit', ['gateCode', EVENT, email])}>${passwordField('Password', 'current-password')}
        <label class="field"><span id="gate-code-label">Code from your authenticator app</span><input class="code" id="gate-code" inputmode="numeric" autocomplete="one-time-code" maxlength="14" required placeholder="000000"></label>
        <div id="gate-error"></div><button class="btn primary" type="submit">Sign in</button></form>
      <div class="links"><button ${on('click', ['forgotPassword', email])}>Forgot your password?</button><button data-on-click="[[&quot;useRecovery&quot;]]">Use a recovery code</button></div>
      <div class="foot-note">This server asks for both. A password never works on its own, and a forgotten one is reset by email plus your code. First time here? A link to set up is in your inbox.</div>`);
    return false;
  }
  gateBox(`<h1>Enter your code</h1>
    <p>Open your authenticator app and type the 6-digit code for <b>${esc(email)}</b>.</p>
    <form ${on('submit', ['gateCode', EVENT, email])}><input class="code" id="gate-code" inputmode="numeric" autocomplete="one-time-code" maxlength="14" required placeholder="000000"><div id="gate-error"></div><button class="btn primary" type="submit">Sign in</button></form>
    <p><b>First time here?</b> If this address has access, a link to set up how you sign in is in your inbox. It works once, for 15 minutes.</p>
    <div class="foot-note">Lost your phone? Type one of your recovery codes instead. <button class="linkish" data-on-click="[[&quot;showGate&quot;]]">Use another address</button></div>`);
  return false;
}
function useRecovery() { const label = $('gate-code-label'); if (label) label.textContent = 'Recovery code'; const f = $('gate-code'); if (f) { f.placeholder = 'XXXX-XXXX'; f.inputMode = 'text'; f.value = ''; f.focus(); } }
async function gateCode(ev, email) {
  ev.preventDefault();
  const body = { email, code: $('gate-code').value };
  if ($('gate-password')) body.password = $('gate-password').value;
  try {
    const d = await api('/auth/email/verify', { method: 'POST', body: JSON.stringify(body), quiet: true });
    if (d.recovery_codes_left !== undefined) snack(`Recovery code used. ${d.recovery_codes_left} left.`);
    location.hash = state.back || '#/overview'; location.reload();
  } catch (e) { gateNotice(e.message, needsSso(e)); $('gate-code').value = ''; $('gate-code').focus(); }
  return false;
}
async function forgotPassword(email) {
  try { await api('/auth/password/forgot', { method: 'POST', body: JSON.stringify({ email }), quiet: true }); } catch (e) { /* the same answer either way */ }
  gateBox(`<h1>Check your email</h1><p>If this address has access, a link to choose a new password is on its way. You'll need your authenticator app as well. The link works once, for 15 minutes.</p><button class="btn" data-on-click="[[&quot;showGate&quot;]]">Back to sign in</button>`);
}
function gatePassword(token) {
  state.resetToken = token || '';
  gateBox(`<div class="stack"><h1>Choose a new password</h1><p>Twelve characters or more. A few ordinary words together are easy to remember and hard to guess.</p></div>
    <form data-on-submit="[[&quot;gateResetPassword&quot;,{&quot;$&quot;:&quot;event&quot;}]]">${passwordField('New password', 'new-password')}
      <label class="field">Code from your authenticator app<input class="code" id="gate-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required placeholder="000000"></label>
      <div id="gate-error"></div><button class="btn primary" type="submit">Save and sign in</button></form>`);
}
async function gateResetPassword(ev) {
  ev.preventDefault();
  try {
    await api('/auth/password/reset', { method: 'POST', body: JSON.stringify({ token: state.resetToken, code: $('gate-code').value, password: $('gate-password').value }), quiet: true });
    location.hash = '#/overview'; location.reload();
  } catch (e) { if (e.body && e.body.data && e.body.data.token) state.resetToken = e.body.data.token; gateNotice(e.message); }
  return false;
}

/* The first visit, from the emailed link: choose a passkey or an authenticator, then keep the recovery codes. */
function gateEnrol(token) {
  // The link does nothing by being opened. Mail scanners open every link in
  // a message, and a link that worked that way would be spent before it was read.
  if (state.methods && state.methods.email && state.methods.email.require === 'passkey') return gateEnrolPasskeyOnly(token);
  const kept = !(state.methods && state.methods.email && state.methods.email.passkeys === false);
  const passkeys = kept && !!window.PublicKeyCredential;
  state.enrolChoice = passkeys ? 'passkey' : 'authenticator';
  gateBox(`<div class="stack"><h1>${kept ? "Choose how you'll sign in" : 'Set up your authenticator app'}</h1><p>${kept ? 'You can add the other one later, from your account menu.' : 'Its code signs you in here until single sign-on is set up.'}</p></div>
    <div class="stack" role="radiogroup" aria-label="Way to sign in" style="gap: 10px">
      ${passkeys ? `<button type="button" class="opt on" role="radio" aria-checked="true" data-way="passkey" data-on-click="[[&quot;pickWay&quot;,&quot;passkey&quot;]]"><span class="radio"></span><span class="head">${icon('key-round', 16)}Passkey${pill('Recommended', 'acc')}</span><span class="desc">The PIN you unlock your computer with. It never leaves your computer, and a fake sign-in page can't use the passkey.</span></button>` : ''}
      <button type="button" class="opt${passkeys ? '' : ' on'}" role="radio" aria-checked="${passkeys ? 'false' : 'true'}" data-way="authenticator" data-on-click="[[&quot;pickWay&quot;,&quot;authenticator&quot;]]"><span class="radio"></span><span class="head">${icon('smartphone', 16)}Authenticator app</span><span class="desc">Type a 6-digit code from an app like Microsoft Authenticator. Works on any phone.</span></button>
    </div>
    <div id="gate-error"></div>
    <button class="btn primary" ${on('click', ['gateEnrolBegin', token || ''])}>Continue</button>
    <div class="foot-note">Next, you'll save ten recovery codes in case a device is lost.</div>`, { step: 'Step 1 of 3', wide: true });
}
function gateEnrolPasskeyOnly(token) {
  state.enrolChoice = 'passkey';
  const able = !!window.PublicKeyCredential;
  gateBox(`<div class="stack"><h1>Make your passkey</h1><p>This server signs people in with passkeys: the PIN you unlock this computer with. It never leaves the computer, and a look-alike sign-in page can't use it.</p></div>
    ${able ? '' : `<div class="notice warn">${icon('alert')}<div>This browser can't make a passkey. Open this link in Chrome, Edge, Safari or Firefox, on a computer with a PIN.</div></div>`}
    <div id="gate-error"></div>
    <button class="btn primary"${able ? '' : ' disabled'} ${on('click', ['gateEnrolBegin', token || ''])}>${icon('key-round', 16)} Make a passkey</button>
    <div class="foot-note">Next, you'll save ten recovery codes in case this computer is lost.</div>`, { step: 'Step 1 of 2', wide: true });
}
function pickWay(way) {
  state.enrolChoice = way;
  document.querySelectorAll('.opt[data-way]').forEach((o) => { const chosen = o.dataset.way === way; o.classList.toggle('on', chosen); o.setAttribute('aria-checked', String(chosen)); });
}
async function gateEnrolBegin(token) {
  try {
    const d = await api('/auth/email/enrol/begin', { method: 'POST', body: JSON.stringify({ token }), quiet: true });
    state.enrol = d;
    if (state.enrolChoice === 'passkey' || d.passkey_only) return enrolPasskey();
    gateBox(`<h1>Scan this with your authenticator app</h1>
      ${d.qr ? `<div class="qr">${d.qr}</div><p>Cannot scan it? Add an account by hand with this key:</p>` : '<p>Add an account in your authenticator app with this key:</p>'}
      <div class="secret">${esc(d.secret.replace(/(.{4})/g, '$1 ').trim())}</div>
      <form data-on-submit="[[&quot;gateEnrolConfirm&quot;,{&quot;$&quot;:&quot;event&quot;}]]"><label class="field">Then type the code it shows<input class="code" id="gate-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required placeholder="000000"></label>${d.passwords ? passwordField('Choose a password', 'new-password') : ''}<div id="gate-error"></div><button class="btn primary" type="submit">Confirm</button></form>
      <div class="foot-note">For ${esc(d.email)}. ${d.passwords ? 'This server asks for a password too, always together with the code.' : "There's no password to set: this app is how you'll sign in."}</div>`, { step: 'Step 2 of 3' });
  } catch (e) { gateNotice(e.message, needsSso(e)); }
}
async function gateEnrolConfirm(ev) {
  ev.preventDefault();
  const body = { ticket: state.enrol.ticket, code: $('gate-code').value };
  if ($('gate-password')) body.password = $('gate-password').value;
  try {
    const d = await api('/auth/email/enrol/confirm', { method: 'POST', body: JSON.stringify(body), quiet: true });
    showRecovery(d.recovery_codes || []);
  } catch (e) {
    if (e.body && e.body.data && e.body.data.ticket) state.enrol.ticket = e.body.data.ticket;
    gateNotice(e.message); $('gate-code').value = ''; $('gate-code').focus();
  }
  return false;
}
async function enrolPasskey() {
  const d = state.enrol;
  try {
    const o = await api('/auth/passkey/enrol/begin', { method: 'POST', body: JSON.stringify({ token: d.ticket }), quiet: true });
    const c = await navigator.credentials.create({ publicKey: creationOptions(o.options) });
    const done = await api('/auth/passkey/enrol/finish', { method: 'POST', body: JSON.stringify({ ticket: d.ticket, credential: credentialJson(c) }), quiet: true });
    showRecovery(done.recovery_codes || []);
  } catch (e) {
    if (e.body && e.body.data && e.body.data.ticket) state.enrol.ticket = e.body.data.ticket;
    const cancelled = e.name === 'NotAllowedError' || e.name === 'AbortError';
    gateBox(`<h1>${cancelled ? 'The passkey was not made' : 'That passkey could not be saved'}</h1>
      <p>${cancelled ? 'Your computer did not finish making it, which happens when its prompt is closed.' : esc(e.message)}</p>
      <button class="btn primary" data-on-click="[[&quot;enrolPasskey&quot;]]">${icon('key-round', 16)} Try again</button>
      ${state.enrol && state.enrol.passkey_only ? '' : `<button class="btn" data-on-click="[[&quot;enrolWithAppInstead&quot;]]">${icon('smartphone', 16)} Use an authenticator app instead</button>`}`, { step: 'Step 2 of 3' });
  }
}
function enrolWithApp() {
  const d = state.enrol;
  gateBox(`<h1>Scan this with your authenticator app</h1>
    ${d.qr ? `<div class="qr">${d.qr}</div><p>Cannot scan it? Add an account by hand with this key:</p>` : '<p>Add an account in your authenticator app with this key:</p>'}
    <div class="secret">${esc(d.secret.replace(/(.{4})/g, '$1 ').trim())}</div>
    <form data-on-submit="[[&quot;gateEnrolConfirm&quot;,{&quot;$&quot;:&quot;event&quot;}]]"><label class="field">Then type the code it shows<input class="code" id="gate-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required placeholder="000000"></label>${d.passwords ? passwordField('Choose a password', 'new-password') : ''}<div id="gate-error"></div><button class="btn primary" type="submit">Confirm</button></form>`, { step: 'Step 2 of 3' });
}
function showRecovery(codes) {
  gateBox(`<h1>Keep these recovery codes</h1>
    <p>Each one signs you in once if you lose your phone or your computer. They are shown now and never again, so put them in a password manager or print them.</p>
    <div class="codes">${codes.map((x) => `<span>${esc(x)}</span>`).join('')}</div>
    <button class="btn" ${on('click', ['copyText', codes.join('\n')])}>${icon('copy', 14)} Copy all</button>
    <button class="btn primary" data-on-click="[[&quot;reloadAt&quot;,&quot;#/overview&quot;]]">I have saved them</button>`, { step: state.enrol && state.enrol.passkey_only ? 'Step 2 of 2' : 'Step 3 of 3' });
}

/* A passkey-only server, and somebody who came in with a code and has none yet. */
function gateMustAddPasskey() {
  const able = !!window.PublicKeyCredential;
  gateBox(`<div class="stack"><h1>Add a passkey to continue</h1><p>This server signs people in with passkeys. Make one now: it is the PIN you unlock this computer with, and it never leaves it. Your code won't be asked for again.</p></div>
    ${able ? '' : `<div class="notice warn">${icon('alert')}<div>This browser can't make a passkey. Open this page in Chrome, Edge, Safari or Firefox, on a computer with a PIN.</div></div>`}
    <div id="gate-error"></div>
    <button class="btn primary"${able ? '' : ' disabled'} data-on-click="[[&quot;makeFirstPasskey&quot;]]">${icon('key-round', 16)} Make a passkey</button>
    <div class="links"><span></span><button data-on-click="[[&quot;signOut&quot;]]">Sign out</button></div>`, { wide: true });
}
async function makeFirstPasskey() {
  try {
    const o = await api('/auth/me/passkeys/begin', { method: 'POST', quiet: true });
    const c = await navigator.credentials.create({ publicKey: creationOptions(o.options) });
    await api('/auth/me/passkeys/finish', { method: 'POST', body: JSON.stringify({ credential: credentialJson(c) }), quiet: true });
    location.reload();
  } catch (e) { if (e.name !== 'NotAllowedError' && e.name !== 'AbortError') gateNotice(e.message); }
}

/* ------------------------------------------------------ confirm it's you */
/* A change that matters (deleting a collection, changing who has access,
   sharing a collection, making a key, changing your own ways in) asks again
   when the last proof is more than ten minutes old, then carries on. */
let stepResolve = null;
function stepUp(ways) {
  return new Promise((resolve) => {
    stepResolve = resolve;
    const sso = ways.includes('sso'); const passkey = ways.includes('passkey') && !!window.PublicKeyCredential; const code = ways.includes('code'); const password = ways.includes('password');
    $('stepup-body').innerHTML = `<h2>Confirm it's you</h2><div class="muted">This change needs a fresh check, because your last one was more than ten minutes ago.</div>
      ${sso ? `<button class="btn primary" data-on-click="[[&quot;stepUpSso&quot;]]">${icon('log-in', 15)} Continue with SSO</button><div class="faint">You'll come back to this page. Then make the change again.</div>` : ''}
      ${passkey ? `<button class="btn${code ? '' : ' primary'}" data-on-click="[[&quot;stepUpPasskey&quot;]]">${icon('key-round', 15)} Use your passkey</button>` : ''}
      ${code ? `<form class="stack" data-on-submit="[[&quot;stepUpCode&quot;,{&quot;$&quot;:&quot;event&quot;}]]"><label class="field">Code from your authenticator app<input class="code" id="stepup-code" inputmode="numeric" autocomplete="one-time-code" maxlength="14" required placeholder="000000"></label><button class="btn primary" type="submit">Confirm</button></form>` : ''}
      ${password ? `<form class="stack" ${on('submit', ['stepUpPassword', EVENT])}><label class="field">Password<input type="password" id="stepup-password" autocomplete="current-password" required></label><button class="btn primary" type="submit">Confirm</button></form>` : ''}
      ${!sso && !passkey && !code && !password ? '<div class="notice warn"><div>There is no way to check it is you on this browser. Sign out and in again, then make the change.</div></div>' : ''}
      <div id="stepup-error"></div>
      <div class="row end"><button class="btn" data-on-click="[[&quot;stepUpDone&quot;,false]]">Cancel</button></div>`;
    openDialog('dlg-stepup');
  });
}
function stepUpDone(ok) { closeDialog('dlg-stepup'); if (stepResolve) { const r = stepResolve; stepResolve = null; r(ok); } }
async function stepUpCode(ev) {
  ev.preventDefault();
  try { await api('/auth/step-up', { method: 'POST', body: JSON.stringify({ code: $('stepup-code').value }), stepped: true }); stepUpDone(true); }
  catch (e) { $('stepup-error').innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(e.message)}</div></div>`; $('stepup-code').value = ''; $('stepup-code').focus(); }
  return false;
}
async function stepUpPassword(ev) {
  ev.preventDefault();
  try { await api('/auth/step-up', { method: 'POST', body: JSON.stringify({ password: $('stepup-password').value }), stepped: true }); stepUpDone(true); }
  catch (e) { $('stepup-error').innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(e.message)}</div></div>`; $('stepup-password').value = ''; $('stepup-password').focus(); }
  return false;
}
async function stepUpPasskey() {
  try {
    const o = await api('/auth/step-up/passkey/begin', { method: 'POST', stepped: true });
    const c = await navigator.credentials.get({ publicKey: requestOptions(o.options) });
    await api('/auth/step-up/passkey/finish', { method: 'POST', body: JSON.stringify({ credential: credentialJson(c) }), stepped: true });
    stepUpDone(true);
  } catch (e) { if (e.name !== 'NotAllowedError' && e.name !== 'AbortError') $('stepup-error').innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(e.message)}</div></div>`; }
}
function stepUpSso() { stepUpDone(false); state.back = location.hash || '#/overview'; startSso('login'); }

/* ------------------------------------------------------- how you sign in */
function wayGroup(title, rows) { return `<div><div class="ways-group">${esc(title)}</div>${rows}</div>`; }
function wayRow(ic, title, sub, action) { return `<div class="way"><span class="ico">${icon(ic, 16)}</span><span><b>${esc(title)}</b><small>${esc(sub)}</small></span><span class="row">${action}</span></div>`; }
async function openWays() {
  closeAccount();
  $('ways-body').innerHTML = '<div class="muted">Loading…</div>'; openDialog('dlg-ways');
  await renderWays();
}
async function renderWays(extra = '') {
  try {
    const [w, s] = await Promise.all([api('/auth/me/ways'), api('/auth/me/sessions')]);
    const me = state.me || {};
    let body = '';
    // Somebody on the People list who came in with single sign-on keeps ways of their own too, and may remove the last of them.
    if (w.listed) body += wayGroup('Single sign-on', wayRow('log-in', 'Your work account', w.sso_at ? `Signed in with it ${agoTs(w.sso_at)}` : 'How you signed in today', pill('Always on', 'acc')));
    if (w.local || w.listed) {
      const keys = w.passkeys || []; const makes = !!window.PublicKeyCredential;
      if (w.own_passkeys !== false) body += wayGroup('Passkeys', keys.length
        ? keys.map((k) => wayRow('key-round', k.name, `Added ${dayOf(k.created_at)}, ${k.last_used ? 'used ' + agoTs(k.last_used) : 'not used yet'}`, `<button class="btn sm" ${on('click', ['removePasskey', k.id])}>Remove</button>`)).join('')
          + (makes ? `<div class="row" style="padding: 6px 0 0 44px"><button class="btn sm" data-on-click="[[&quot;addPasskey&quot;]]">${icon('plus', 14)} Add a passkey</button></div>` : '')
        : wayRow('key-round', 'None yet', makes ? 'The PIN you unlock your computer with' : "This browser can't make one", makes ? '<button class="btn sm" data-on-click="[[&quot;addPasskey&quot;]]">Add a passkey</button>' : ''));
      if (!w.require_passkey) body += wayGroup('Authenticator app', w.authenticator
        ? wayRow('smartphone', `Set up ${dayOf(w.authenticator.set_up_at)}`, w.authenticator.used_at ? `Used ${agoTs(w.authenticator.used_at)}` : 'Not used yet', `<button class="btn sm" data-on-click="[[&quot;replaceAuthenticator&quot;]]">Replace</button>${w.listed || keys.length ? `<button class="btn sm" ${on('click', ['removeAuthenticator'])}>Remove</button>` : ''}`)
        : wayRow('smartphone', 'Not set up', 'A 6-digit code from an app on your phone', '<button class="btn sm" data-on-click="[[&quot;replaceAuthenticator&quot;]]">Set up</button>'));
      if (w.local) body += wayGroup('Recovery codes', wayRow('life-buoy', `${w.recovery_codes_left} of 10 left`, 'Each one works once', '<button class="btn sm" data-on-click="[[&quot;newRecoveryCodes&quot;]]">Make new codes</button>'));
      if (w.passwords) body += wayGroup('Password', wayRow('lock', w.password ? `Set ${dayOf(w.password.set_at)}` : 'Not set', 'Asked for together with your code', `<button class="btn sm" data-on-click="[[&quot;changePassword&quot;]]">${w.password ? 'Change' : 'Set a password'}</button>`));
    }
    const sessions = s.sessions || [];
    body += wayGroup('Signed in on', sessions.map((x) => wayRow('monitor', x.device, `${x.current ? 'This browser, ' : ''}active ${agoTs(x.last_seen)}${x.address ? ', from ' + x.address : ''}`, x.current ? pill('This one', 'acc') : `<button class="btn sm" ${on('click', ['endSession', x.id])}>Sign out</button>`)).join('')
      + (sessions.length > 1 ? '<div class="row" style="padding: 6px 0 0 44px"><button class="btn sm" data-on-click="[[&quot;endOtherSessions&quot;]]">Sign out everywhere else</button></div>' : ''));
    $('ways-body').innerHTML = `<div class="head"><h2>How you sign in</h2><button class="btn icon" data-on-click="[[&quot;closeDialog&quot;,&quot;dlg-ways&quot;]]" aria-label="Close">${icon('x', 16)}</button></div>
      <div class="muted">${esc(me.email || me.name || '')}. ${w.method === 'break_glass' ? "You came in by emergency sign-in: its password is kept in the key vault, and it ends when emergency sign-in is turned off."
        : w.method === 'developer' ? "You came in by Developer Access: its accounts and password are in this machine's settings, and it works on this machine only."
        : w.listed ? 'You sign in with your work account. An authenticator app you add here works too, and you can remove it any time.'
        : !w.local ? 'You sign in with your company account; its password and second step are set with your company.'
        : w.require_passkey ? "This server signs people in with passkeys. Keep one on a second device, so one lost device can't lock you out."
        : w.own_passkeys === false ? "Your authenticator app signs you in. Keep your recovery codes somewhere safe." : "Keep at least two ways in, so one lost device can't lock you out."}</div>${extra}${body}`;
  } catch (e) { $('ways-body').innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(e.message)}</div></div>`; }
}
async function addPasskey() {
  try {
    const o = await api('/auth/me/passkeys/begin', { method: 'POST' });
    const c = await navigator.credentials.create({ publicKey: creationOptions(o.options) });
    await api('/auth/me/passkeys/finish', { method: 'POST', body: JSON.stringify({ credential: credentialJson(c) }) });
    snack('Passkey added'); renderWays();
  } catch (e) { if (e.name !== 'NotAllowedError' && e.name !== 'AbortError') snack(e.message); }
}
function removePasskey(id) { sure('Remove this passkey? The device it is on can no longer sign you in.', 'Remove', async () => { try { await api(`/auth/me/passkeys/${encodeURIComponent(id)}`, { method: 'DELETE' }); snack('Removed'); } catch (e) { snack(e.message); } renderWays(); }); }
function removeAuthenticator() { sure('Remove your authenticator app? Its codes stop working here.', 'Remove', async () => { try { await api('/auth/me/authenticator', { method: 'DELETE' }); snack('Removed'); } catch (e) { snack(e.message); } renderWays(); }); }
async function replaceAuthenticator() {
  try {
    const d = await api('/auth/me/authenticator/begin', { method: 'POST' });
    renderWays(`<div class="card"><div class="head"><h2>Scan this with your authenticator app</h2></div>${d.qr ? `<div class="qr">${d.qr}</div>` : ''}<div class="secret">${esc(d.secret.replace(/(.{4})/g, '$1 ').trim())}</div>
      <form class="row" data-on-submit="[[&quot;confirmAuthenticator&quot;,{&quot;$&quot;:&quot;event&quot;}]]"><input class="code" id="ways-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required placeholder="000000" style="flex-grow: 1"><button class="btn primary" type="submit">Confirm</button></form><div class="faint">The old one keeps working until this one is confirmed.</div></div>`);
  } catch (e) { snack(e.message); }
}
async function confirmAuthenticator(ev) {
  ev.preventDefault();
  try { await api('/auth/me/authenticator/confirm', { method: 'POST', body: JSON.stringify({ code: $('ways-code').value }) }); snack('Authenticator app set up'); renderWays(); }
  catch (e) { snack(e.message); }
  return false;
}
function newRecoveryCodes() {
  sure('Make ten new recovery codes? The ones you have now stop working.', 'Make new codes', async () => {
    try {
      const d = await api('/auth/me/recovery-codes', { method: 'POST' });
      const codes = d.recovery_codes || [];
      renderWays(`<div class="card"><div class="head"><h2>Your new recovery codes</h2></div><div class="muted">Shown now and never again. Put them in a password manager or print them.</div><div class="codes">${codes.map((x) => `<span>${esc(x)}</span>`).join('')}</div><div class="row"><button class="btn sm" ${on('click', ['copyText', codes.join('\n')])}>${icon('copy', 14)} Copy all</button></div></div>`);
    } catch (e) { snack(e.message); }
  });
}
function changePassword() {
  renderWays(`<div class="card"><div class="head"><h2>Choose a new password</h2></div><form class="stack" data-on-submit="[[&quot;savePassword&quot;,{&quot;$&quot;:&quot;event&quot;}]]">${passwordField('New password', 'new-password')}<div class="faint">Twelve characters or more. A few ordinary words together are easy to remember and hard to guess.</div><div class="row"><button class="btn primary" type="submit">Save</button></div></form></div>`);
}
async function savePassword(ev) {
  ev.preventDefault();
  try { await api('/auth/me/password', { method: 'POST', body: JSON.stringify({ password: $('gate-password').value }) }); snack('Password saved'); renderWays(); }
  catch (e) { snack(e.message); }
  return false;
}
async function endSession(id) { try { await api(`/auth/me/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }); snack('Signed out there'); } catch (e) { snack(e.message); } renderWays(); }
async function endOtherSessions() { try { const d = await api('/auth/me/sessions/end-others', { method: 'POST' }); snack(`Signed out of ${d.ended} other ${d.ended === 1 ? 'place' : 'places'}`); } catch (e) { snack(e.message); } renderWays(); }

/* ------------------------------------------------------ keys for scripts */
async function openKeys() {
  closeAccount();
  $('keys-body').innerHTML = '<div class="muted">Loading…</div>'; openDialog('dlg-keys');
  await renderKeys();
}
async function renderKeys(made) {
  try {
    const d = await api('/api/v1/keys');
    const means = (role) => (d.roles.find((r) => r.role === role) || {}).means || role;
    // A key that reaches everything, until it is revoked, is the quiet case
    // and says nothing. A narrower one says so under its name.
    // A key can end years out, and a day and month with no year reads as a
    // date just gone, so the year is there whenever it is not this one.
    const endsOn = (secs) => {
      const day = new Date(secs * 1000);
      const thisYear = day.getFullYear() === new Date().getFullYear();
      return day.toLocaleDateString([], thisYear ? { day: 'numeric', month: 'short' } : { day: 'numeric', month: 'short', year: 'numeric' });
    };
    const scopeOf = (k) => {
      const bits = [];
      if ((k.collections || []).length) bits.push(k.collections.map(esc).join(', '));
      if (k.expires_at) bits.push(k.expired ? 'expired' : `until ${endsOn(k.expires_at)}`);
      return bits.length ? `<div class="faint" style="margin-top: 2px">${bits.join(' · ')}</div>` : '';
    };
    const rows = d.keys.length ? `<div class="table"><div class="tr head t-keys"><span>Name</span><span>Can</span><span>Key</span><span>Last used</span><span></span></div>${d.keys.map((k) => `<div class="tr t-keys"><span>${esc(k.name)}${k.expired ? ' ' + pill('Expired', 'warn') : ''}${scopeOf(k)}</span><span class="muted">${esc(means(k.role))}</span>${mono(k.prefix + '…', 'faint')}<span class="muted">${k.last_used ? agoTs(k.last_used) : 'never'}</span><span class="row end"><button class="btn sm danger" ${on('click', ['revokeKey', k.id, k.name])}>Revoke</button></span></div>`).join('')}</div>` : '<div class="muted">No keys yet.</div>';
    $('keys-body').innerHTML = `<div class="head"><h2>API keys for scripts</h2><button class="btn icon" data-on-click="[[&quot;closeDialog&quot;,&quot;dlg-keys&quot;]]" aria-label="Close">${icon('x', 16)}</button></div>
      <div class="muted">A key lets a script use this server without signing in. It is shown once, when it is made, and kept only as a hash. Revoke a key the moment nothing needs it.</div>
      ${made ? `<div class="notice ok">${icon('check')}<div><b>Copy this key now.</b> It will not be shown again.<div class="keyshow" style="margin-top: 8px">${esc(made)}</div><div class="row" style="margin-top: 8px"><button class="btn sm" ${on('click', ['copyText', made])}>${icon('copy', 14)} Copy</button></div></div></div>` : ''}
      ${rows}
      <form class="keyform" data-on-submit="[[&quot;makeKey&quot;,{&quot;$&quot;:&quot;event&quot;}]]">
        <label class="field">What uses it<input id="key-name" placeholder="nightly-ingest" required></label>
        <label class="field">What it may do<select id="key-role">${d.roles.map((r) => `<option value="${esc(r.role)}"${r.role === 'searcher' ? ' selected' : ''}>${esc(r.means)}</option>`).join('')}</select></label>
        <label class="field">Which collections<select id="key-cols" multiple size="3">${state.collections.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('')}</select><span class="faint">Pick none for every collection.</span></label>
        <label class="field">Until<select id="key-days"><option value="">It is revoked</option><option value="30">30 days</option><option value="90">90 days</option><option value="365">A year</option></select></label>
        <button class="btn primary" type="submit">${icon('plus', 14)} Make a key</button>
      </form>`;
  } catch (e) { $('keys-body').innerHTML = `<div class="notice bad">${icon('x')}<div>${esc(e.message)}</div></div>`; }
}
async function makeKey(ev) {
  ev.preventDefault();
  const picked = [...$('key-cols').selectedOptions].map((o) => o.value).filter(Boolean);
  const days = Number($('key-days').value) || null;
  try { const d = await api('/api/v1/keys', { method: 'POST', body: JSON.stringify({ name: $('key-name').value.trim(), role: $('key-role').value, collections: picked.length ? picked : null, expires_in_days: days }) }); renderKeys(d.key); }
  catch (e) { snack(e.message); }
  return false;
}
function revokeKey(id, name) { sure(`Revoke the key ${name}? Anything using it stops working at once.`, 'Revoke', async () => { try { await api(`/api/v1/keys/${encodeURIComponent(id)}`, { method: 'DELETE' }); snack('Revoked'); } catch (e) { snack(e.message); } renderKeys(); }); }

/* -------------------------------------------------------------- access */
async function loadAccess() {
  const el = $('access-body');
  let people = null;
  if (state.me && state.me.people) { try { people = await api(`/auth/people?${pageQuery('people')}`); } catch (e) { people = null; } }
  const log = await api(`/api/v1/access?${pageQuery('log')}`);
  el.innerHTML = `<div class="grid lay-trends" id="ac-trends" hidden></div><div id="people-card">${people ? peopleCardHtml(people) : `<div class="notice info">${icon('info')}<div><b>People and their roles come from your identity provider.</b> Who may sign in, and as what, is set there; this page shows who did.</div></div>`}</div>
    <div class="card"><div class="head"><h2>What each role may do</h2></div>
      <div class="table"><div class="tr head t-grants"><span>Action</span><span>Viewer</span><span>Operator</span><span>Admin</span></div>
      ${(log.grants || []).map((g) => `<div class="tr t-grants"><span>${esc(g.means)}</span><span>${accTick(g.viewer)}</span><span>${accTick(g.operator)}</span><span>${accTick(g.admin)}</span></div>`).join('')}</div></div>
    <div id="log-card">${logCardHtml(log)}</div>`;
  loadAccessTrends();
}
const accTick = (yes) => yes ? `<span class="pill ok">${icon('check', 12)} yes</span>` : '<span class="faint">no</span>';
const accWhen = (at) => ago(new Date(at * 1000).toISOString());
const accKind = (ev) => ['denied', 'signin_failed', 'locked'].includes(ev) ? 'bad' : ev === 'signin' || ev === 'enrolled' ? 'ok' : ev.startsWith('person') || ev.startsWith('key_') || ['authenticator_reset', 'recovery_code_used', 'visibility_changed', 'passkey_removed'].includes(ev) ? 'warn' : 'info';
const accWays = (x) => x.passkeys || x.authenticator || x.last_sso_at ? `<span class="row" style="gap: 4px">${x.last_sso_at ? pill('SSO', 'info') : ''}${x.passkeys ? pill(`${x.passkeys} passkey${x.passkeys === 1 ? '' : 's'}`, 'ok') : ''}${x.authenticator ? pill('authenticator', 'ok') : ''}</span>` : pill('not yet', '');
function peopleCardHtml(people) {
  const pg = pageState('people');
  const rows = people.people.map((x) => `<div class="tr t-people">${mono(x.email)}<span><select ${on('change', ['setRole', x.email, VALUE])} aria-label="Role">${people.roles.map((r) => `<option ${r === x.role ? 'selected' : ''}>${r}</option>`).join('')}</select></span><span>${x.role === 'admin' ? '<span class="faint">always</span>' : x.role === 'operator' ? `<label class="row" style="gap: 6px"><input type="checkbox" ${(x.grants || []).includes('document.read') ? 'checked' : ''} ${on('change', ['setDocumentRead', x.email, x.role, CHECKED])}> may open</label>` : '<span class="faint">no</span>'}</span><span>${accWays(x)}</span><span class="row" style="justify-content: flex-end">${state.me && state.me.ownWays ? `<button class="btn sm" ${on('click', ['resetPerson', x.email])}>Reset sign-in</button>` : ''}<button class="btn sm" ${on('click', ['removePerson', x.email])}>Remove</button></span></div>`).join('');
  return `<div class="card"><div class="head"><h2>People</h2><button class="btn sm primary" data-on-click="[[&quot;openPerson&quot;]]">${icon('user-plus', 14)} Add somebody</button></div>
      ${pager('people', { placeholder: 'Find a person', q: pg.q, total: people.total || people.people.length, from: people.offset || 0, count: people.people.length })}
      <div class="table"><div class="tr head t-people"><span>Address</span><span>Role</span><span>Whole documents</span><span>Signs in with</span><span></span></div>
      ${rows || `<div class="tr t-one"><span class="muted">${pg.q.trim() ? 'Nobody is called that.' : 'Nobody yet.'}</span></div>`}</div>
      <div class="faint">Somebody new signs in with their address, gets a link by email, and ${state.me && state.me.passkeys === false ? 'sets up an authenticator app' : 'chooses a passkey or an authenticator app'}. A reset sends them round again, and signs them out everywhere.</div></div>`;
}
function logCardHtml(log) {
  const pg = pageState('log');
  const rows = log.where === 'stdout' ? `<div class="tr t-one"><span class="muted">This server writes its access log to its output, where the platform it runs on collects it. Read it there.</span></div>` : log.records.length ? log.records.map((r) => `<div class="tr t-access"><span class="muted">${accWhen(r.at)}</span><span>${pill(r.event.replace(/_/g, ' '), accKind(r.event))}</span>${mono(r.who || '')}<span class="muted">${esc(r.role || '')}</span>${mono(r.collection || '', 'faint')}<span class="faint">${esc(r.by || r.reason || r.action || '')}</span></div>`).join('') : `<div class="tr t-one"><span class="muted">${pg.q.trim() ? 'No line says that.' : 'Nothing yet'}</span></div>`;
  return `<div class="card"><div class="head"><h2>Access log</h2><span class="faint">who signed in, who read what, who was refused</span></div>
      ${log.where === 'stdout' ? '' : pager('log', { placeholder: 'Find by who, event or collection', q: pg.q, total: log.total || 0, from: log.offset || 0, count: log.records.length })}
      <div class="table"><div class="tr head t-access"><span>When</span><span>Event</span><span>Who</span><span>Role</span><span>Collection</span><span>By</span></div>${rows}</div>
      <div class="faint">The log names people, actions and collections. It never holds a query, a chunk's text or what a search returned.</div></div>`;
}
PAGERS.people = async () => { const holder = $('people-card'); if (!holder) return; try { holder.innerHTML = peopleCardHtml(await api(`/auth/people?${pageQuery('people')}`)); } catch (e) { snack(e.message); } };
PAGERS.log = async () => { const holder = $('log-card'); if (!holder) return; try { holder.innerHTML = logCardHtml(await api(`/api/v1/access?${pageQuery('log')}`)); } catch (e) { snack(e.message); } };
/* Who reads most, and which keys sit idle. The row is left out, not drawn
   empty, when the log goes to the server's output. Keys only for somebody
   who may manage them. */
async function loadAccessTrends() {
  const holder = $('ac-trends'); if (!holder) return;
  const pg = pageState('readers');
  try {
    const d = await api(`/api/v1/access/readers?days=14&top=${PAGE}&offset=${pg.page * PAGE}${pg.q.trim() ? `&q=${encodeURIComponent(pg.q.trim())}` : ''}`);
    if (!d.available) return;
    let keys = null;
    if (can('keys.manage')) { try { keys = (await api('/api/v1/keys')).keys || []; } catch (e) { keys = null; } }
    // Sign-ins against refusals: about people, so drawn here and not on the Overview.
    let daily = null;
    try { daily = await api('/api/v1/access/daily?days=14', { quiet: true }); } catch (e) { daily = null; }
    if (!$('ac-trends')) return;
    holder.hidden = false;
    trAccess(holder, d.readers, d.days, keys, { q: pg.q, total: d.total || d.readers.length, offset: d.offset || 0 }, daily && daily.available ? daily : null);
  } catch (e) { holder.hidden = true; }
}
PAGERS.readers = () => loadAccessTrends();
function openPerson() { $('person-email').value = ''; $('person-role').value = 'viewer'; openDialog('dlg-person'); }
function addPerson() { const email = $('person-email').value.trim(); if (!email) return; closeDialog('dlg-person'); savePerson(email, $('person-role').value); }
function sure(text, label, then) { $('confirm-text').textContent = text; $('confirm-btn').textContent = label; $('confirm-btn').onclick = async () => { closeDialog('dlg-confirm'); await then(); }; openDialog('dlg-confirm'); }
async function savePerson(email, role, grants) { try { await api('/auth/people', { method: 'POST', body: JSON.stringify(grants === undefined ? { email, role } : { email, role, grants }) }); snack('Saved'); } catch (e) { snack(e.message); } loadAccess(); }
function setRole(email, role) { savePerson(email, role); }
function resetPerson(email) { sure(`Reset how ${email} signs in? Their passkeys, authenticator and recovery codes are forgotten, they are signed out everywhere, and they set up again from a new link by email.`, 'Reset', async () => { try { await api(`/auth/people/${encodeURIComponent(email)}/reset`, { method: 'POST' }); snack('Reset'); } catch (e) { snack(e.message); } loadAccess(); }); }
function removePerson(email) { sure(`Remove ${email}? They are signed out and can no longer sign in.`, 'Remove', async () => { try { await api(`/auth/people/${encodeURIComponent(email)}`, { method: 'DELETE' }); snack('Removed'); } catch (e) { snack(e.message); } loadAccess(); }); }

/* ------------------------------------------------------------ settings */
/* Settings live in the account menu, top right, under who is signed in. What
   the Overview already says (collections, vectors, size on disk) is not
   repeated in it, and the version is beside the line in the sidebar. */
async function openAccount() {
  const menu = $('account-menu'); const btn = $('btn-account');
  menu.hidden = false; btn.setAttribute('aria-expanded', 'true');
  applyTheme();
  const first = menu.querySelector('a:not([hidden]), button:not([hidden])'); if (first) first.focus();
  try { await Promise.all([loadInfo(), loadAuth()]); } catch (e) { /* the menu still opens */ }
  const access = state.me ? null : (state.authEnabled ? 'API key required' : 'open, no key set');
  $('set-server').innerHTML = [['Server', location.host], ['Storage', (state.info && state.info.storage_backend) || 'sqlite'], ...(access ? [['Access', access]] : [])]
    .map(([k, v]) => `<span class="k">${esc(k)}</span><span class="v">${esc(v)}</span>`).join('');
}
function closeAccount(refocus) {
  const menu = $('account-menu'); if (menu.hidden) return;
  menu.hidden = true; $('btn-account').setAttribute('aria-expanded', 'false');
  if (refocus) $('btn-account').focus();
}

/* ------------------------------------------------------------ realtime */
let ws = null; let wsRetry = 1000;
function connectWs() {
  try {
    ws = new WebSocket(at('/ws').replace(/^http/, 'ws'));
    ws.onopen = () => { wsRetry = 1000; $('pill-live').className = 'pill ok'; $('pill-live').textContent = 'Live'; };
    ws.onclose = () => { $('pill-live').className = 'pill'; $('pill-live').textContent = 'Reconnecting'; setTimeout(connectWs, wsRetry); wsRetry = Math.min(wsRetry * 2, 30000); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
    ws.onmessage = (ev) => { let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; } const type = msg.event || msg.type; if (['collection_created', 'collection_deleted', 'points_added', 'points_deleted', 'index_rebuilt'].includes(type)) { const name = msg.data?.collection || msg.data?.name; if (name) delete state.health[name]; if (['overview', 'collections', 'collection'].includes(state.page)) refresh(); } };
  } catch (e) { $('pill-live').className = 'pill'; $('pill-live').textContent = 'Offline'; }
}

/* ---------------------------------------------------------------- boot */
async function boot() {
  applyBrand();
  applyTheme();
  document.querySelectorAll('[data-icon]').forEach((el) => { el.innerHTML = icon(el.dataset.icon, parseInt(el.dataset.size) || 17) + el.innerHTML; });
  $('btn-key').onclick = () => { closeAccount(); openKeyDialog(); };
  $('btn-account').onclick = (e) => { e.stopPropagation(); if ($('account-menu').hidden) openAccount(); else closeAccount(true); };
  document.addEventListener('click', (e) => { if (!e.target.closest('.menu-wrap')) closeAccount(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAccount(true); });
  window.addEventListener('hashchange', () => closeAccount());
  $('mi-access').onclick = () => closeAccount();
  $('mi-keys').onclick = openKeys; $('mi-ways').onclick = openWays; $('mi-about').onclick = openAbout; $('mi-signout').onclick = signOut;
  $('btn-signin').onclick = () => { state.back = location.hash || '#/overview'; showGate(); };
  const app = document.querySelector('.app');
  const drawer = window.matchMedia('(max-width: 900px)');
  // Two controls, one job each. The one in the sidebar's header folds it to a
  // rail on a desktop and remembers. The one in the top bar opens it as a
  // drawer on a narrow screen, where the sidebar is not on screen to hold it.
  const showRail = () => {
    const collapsed = app.classList.contains('collapsed');
    const say = collapsed ? 'Expand the sidebar' : 'Collapse the sidebar';
    $('side-toggle').innerHTML = icon(collapsed ? 'panel-open' : 'panel-close', 18);
    $('side-toggle').setAttribute('aria-label', say);
    $('side-toggle').title = say;
    $('side-toggle').setAttribute('aria-expanded', String(!collapsed));
    $('hamburger').setAttribute('aria-expanded', String($('side').classList.contains('open')));
  };
  try { if (localStorage.getItem('vectrixdb.side') === 'collapsed') app.classList.add('collapsed'); } catch (e) {}
  showRail();
  $('side-toggle').onclick = () => {
    app.classList.toggle('collapsed');
    try { localStorage.setItem('vectrixdb.side', app.classList.contains('collapsed') ? 'collapsed' : 'open'); } catch (e) {}
    showRail();
  };
  $('hamburger').onclick = () => { $('side').classList.toggle('open'); showRail(); };
  const closeDrawer = () => { $('side').classList.remove('open'); showRail(); };
  $('side-close').onclick = closeDrawer;
  $('scrim').onclick = closeDrawer;
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('side').classList.contains('open')) closeDrawer(); });
  drawer.addEventListener('change', () => { $('side').classList.remove('open'); showRail(); });
  // A dialog closes from a click beside it or Escape. "Confirm it's you" also says it was cancelled.
  const shut = (b) => { if (b.id === 'dlg-stepup') stepUpDone(false); else b.classList.remove('open'); };
  document.querySelectorAll('.backdrop').forEach((b) => b.addEventListener('click', (e) => { if (e.target === b) shut(b); }));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') document.querySelectorAll('.backdrop.open').forEach(shut); if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) { e.preventDefault(); go('#/search'); setTimeout(() => $('s-query').focus(), 50); } });
  setupDrop();
  // The version is said only to somebody signed in, or to anybody when nobody signs in.
  try { const r = await api('/'); state.version = r.version || ''; document.body.dataset.version = r.version || ''; } catch (e) {}
  window.addEventListener('hashchange', () => { if (/^#\/(enrol|signin|password)/.test(location.hash)) showGate(); else if (location.hash.startsWith('#/break-glass')) gateBreakGlass(); else if (!state.gated) route(); });
  watchTables();
  if (!(await whoAmI())) return;
  await loadAuth();
  route();
  // A guest has no live feed: it would name collections that are not shared. The server answered, so it is live.
  if (state.guest) { $('pill-live').className = 'pill ok'; $('pill-live').textContent = 'Live'; } else connectWs();
}
document.addEventListener('DOMContentLoaded', boot);
