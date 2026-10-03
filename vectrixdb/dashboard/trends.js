/* Counts a day, as bars and lines, and lists as rows. The Overview draws
   searches with the policy's refusals over them and search time from the
   access log, and what was written across every collection with the part
   below the quality line in red; Audit draws decisions by outcome from the
   audit trail, and the same by collection; Access draws sign-ins against
   refusals and who reads most; a collection draws its growth and its builds.
   Every list of rows that can grow turns ten a page.
   Every chart leads with a headline: the number and one line that compares
   it with the week before. The scale hugs the data, the latest day stands
   out and earlier days sit back, and every bar says its day and count on
   hover. Drawn in script at the width they are shown at, so the text stays
   one size on a phone and on a desktop, and drawn again when the window
   changes. Colour is a class, so both themes get it from the stylesheet.
   Nothing here asks the server for anything: app.js hands it the numbers. */

const TR = { drawn: [], timer: null, byc: { rows: [], q: '', page: 0 }, keys: null, builds: null, batch: null };
const TR_H = 150;
const TR_PAGE = 10;

/* The top of the scale and the step between its lines: the smallest step,
   on a number people count in, whose three multiples cover the most, so the
   tallest bar reaches near the top; two steps as the floor so a run of ones
   is not a wall. One scale places the bars, the lines and the labels, so a
   label always names a value the chart reaches. */
function trScale(most) {
  const steps = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 50000, 100000];
  const step = steps.find((s) => s * 3 >= most) || Math.ceil(most / 3);
  return { step, top: Math.max(step * Math.ceil(most / step), step * 2) };
}

/* Room on the left for the widest tick label: about six pixels a character, and a little more. */
const trLeft = (top) => Math.max(30, 12 + 6.2 * fmtNum(top).length);

const trDay = (iso) => new Date(iso + 'T00:00:00Z').toLocaleDateString([], { month: 'short', day: 'numeric', timeZone: 'UTC' });

/* The frame every day chart shares: the grid lines and their labels, the
   baseline, and the y function that places a value. */
function trFrame(W, H, L, R, T, B, top, step) {
  const plotH = H - T - B;
  const y = (v) => T + plotH - plotH * v / top;
  let g = '';
  for (let tick = step; tick <= top; tick += step) {
    g += `<line x1="${L}" y1="${y(tick).toFixed(1)}" x2="${W - R}" y2="${y(tick).toFixed(1)}" class="ev-grid"></line><text x="${L - 6}" y="${(y(tick) + 3.5).toFixed(1)}" text-anchor="end" class="tr-tick">${fmtNum(tick)}</text>`;
  }
  g += `<line x1="${L}" y1="${y(0).toFixed(1)}" x2="${W - R}" y2="${y(0).toFixed(1)}" class="tr-base"></line>`;
  return { g, y };
}

/* Day labels along the bottom: the first, the last, and three between; on a
   chart too narrow for five, one between, so no two labels touch. */
function trDayLabels(days, x, W, R, H) {
  const last = days.length - 1, between = x(last) - x(0) < 260 ? 2 : 4;
  return [...new Set([...Array(between).keys()].map((n) => Math.round(n * last / between)).concat(last))].map((d) => {
    // A label sits under the middle of its day, except where that would push it
    // past the edge of a narrow chart: there it ends at the edge instead.
    const mid = x(d), tight = mid + 20 > W - R;
    return `<text x="${(tight ? W - R : mid).toFixed(1)}" y="${H - 5}" text-anchor="${tight ? 'end' : 'middle'}" class="tr-tick">${esc(trDay(days[d]))}</text>`;
  }).join('');
}

/* series: [{ cls, values }]. Stacked, they pile up; otherwise side by side.
   The latest day is drawn at full strength and the days before it sit back,
   and a single series writes its latest value above its bar. overlay: a
   line drawn over the bars, with a dot on each day it has a value and its
   latest value written at the right. */
function trBars(width, days, series, stacked, label, overlay) {
  const most = Math.max(1, ...days.map((_, d) => stacked ? series.reduce((sum, s) => sum + (s.values[d] || 0), 0) : Math.max(...series.map((s) => s.values[d] || 0))), ...(overlay ? overlay.values : []));
  const { step, top } = trScale(most);
  // The left margin fits the widest tick label, so a count in the thousands is not cut off.
  // T leaves room for the latest value written above the tallest bar: a bar that reaches the top of the scale
  // put its label's baseline at 5, so the top half of the digits was outside the picture.
  const R = overlay ? 30 : 4, L = trLeft(top), T = 18, B = 20, W = Math.max(220, Math.floor(width)), H = TR_H;
  const plotW = W - L - R, slot = plotW / days.length;
  let { g, y } = trFrame(W, H, L, R, T, B, top, step);
  const last = days.length - 1;
  const x = (d) => L + d * slot + slot / 2;
  days.forEach((_, d) => {
    const x0 = L + d * slot, back = d === last ? '' : ' tr-back';
    const tip = `<title>${esc(trDay(days[d]))}: ${series.map((s) => fmtNum(s.values[d] || 0)).join(' + ')}</title>`;
    if (stacked) {
      const w = slot * 0.62; let from = 0;
      series.forEach((s, n) => {
        const v = s.values[d] || 0;
        // The first series sits back, and any marked back; one marked front never does: what is denied, refused or below the line stays at full strength on every day.
        if (v) g += `<rect x="${(x0 + (slot - w) / 2).toFixed(1)}" y="${y(from + v).toFixed(1)}" width="${w.toFixed(1)}" height="${(y(from) - y(from + v)).toFixed(1)}" class="${s.cls}${(n === 0 || s.back) && !s.front ? back : ''}">${tip}</rect>`;
        from += v;
      });
    } else {
      const w = slot * 0.7 / series.length;
      series.forEach((s, n) => {
        const v = s.values[d] || 0; if (!v) return;
        const h = Math.max(y(0) - y(v), 1.5);
        g += `<rect x="${(x0 + slot * 0.15 + n * w).toFixed(1)}" y="${(y(0) - h).toFixed(1)}" width="${Math.max(w - 1, 1).toFixed(1)}" height="${h.toFixed(1)}" rx="2" class="${s.cls}${back}">${tip}</rect>`;
      });
    }
  });
  // A lone series writes its latest value above its bar; with an overlay both values are written at the right instead.
  if (!stacked && series.length === 1 && series[0].values[last] && !overlay) {
    g += `<text x="${x(last).toFixed(1)}" y="${(y(series[0].values[last]) - 5).toFixed(1)}" text-anchor="middle" class="tr-val ${series[0].cls}">${fmtNum(series[0].values[last])}</text>`;
  }
  if (overlay) {
    g += `<polyline points="${overlay.values.map((v, d) => `${x(d).toFixed(1)},${y(v || 0).toFixed(1)}`).join(' ')}" class="tr-line ${overlay.cls}"></polyline>`;
    overlay.values.forEach((v, d) => { if (v) g += `<circle cx="${x(d).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.5" class="tr-dot ${overlay.cls}"><title>${esc(trDay(days[d]))}: ${fmtNum(v)} ${esc(overlay.word || '')}</title></circle>`; });
    g += `<text x="${(x(last) + 7).toFixed(1)}" y="${(y(series[0].values[last] || 0) + 4).toFixed(1)}" class="tr-val ${series[0].cls}">${fmtNum(series[0].values[last] || 0)}</text>`;
    g += `<text x="${(x(last) + 7).toFixed(1)}" y="${(y(overlay.values[last] || 0) - 6).toFixed(1)}" class="tr-val ${overlay.cls}">${fmtNum(overlay.values[last] || 0)}</text>`;
  }
  g += trDayLabels(days, x, W, R, H);
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}" class="tr-svg">${g}</svg>`;
}

/* Lines over the same days, on a scale made the same way. series: [{ cls,
   values, area, dashed, unit }], where a value may be null: a day with
   nothing measured breaks the line, and is not drawn as a nought, which
   would say the day was fast. A series with area is filled underneath;
   each series writes its latest value at the right instead of a legend. */
function trLines(width, days, series, label) {
  const most = Math.max(1, ...series.flatMap((s) => s.values.filter((v) => v !== null && v !== undefined)));
  const { step, top } = trScale(most);
  const L = trLeft(top), R = 46, T = 10, B = 20, W = Math.max(220, Math.floor(width)), H = TR_H;
  const plotW = W - L - R, slot = plotW / days.length;
  const x = (d) => L + d * slot + slot / 2;
  let { g, y } = trFrame(W, H, L, R, T, B, top, step);
  series.forEach((s) => {
    let run = [], lastAt = null, first = null;
    const flush = () => {
      if (run.length > 1) {
        if (s.area) g += `<polygon points="${run[0].split(',')[0]},${y(0).toFixed(1)} ${run.join(' ')} ${run[run.length - 1].split(',')[0]},${y(0).toFixed(1)}" class="tr-area ${s.cls}"></polygon>`;
        g += `<polyline points="${run.join(' ')}" class="tr-line ${s.cls}${s.dashed ? ' tr-dash' : ''}"></polyline>`;
      } else if (run.length === 1) { const [cx, cy] = run[0].split(','); g += `<circle cx="${cx}" cy="${cy}" r="2" class="tr-dot ${s.cls}"></circle>`; }
      run = [];
    };
    s.values.forEach((v, d) => { if (v === null || v === undefined) { flush(); return; } run.push(`${x(d).toFixed(1)},${y(v).toFixed(1)}`); lastAt = d; if (first === null) first = d; });
    flush();
    if (lastAt !== null) {
      g += `<circle cx="${x(lastAt).toFixed(1)}" cy="${y(s.values[lastAt]).toFixed(1)}" r="3" class="tr-dot ${s.cls}"></circle>`;
      g += `<text x="${(x(lastAt) + 7).toFixed(1)}" y="${(y(s.values[lastAt]) + 4).toFixed(1)}" class="tr-val ${s.cls}">${fmtNum(Math.round(s.values[lastAt]))}${esc(s.unit || '')}</text>`;
    }
  });
  g += trDayLabels(days, x, W, R, H);
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}" class="tr-svg">${g}</svg>`;
}

const trKeys = (pairs) => pairs.length ? `<div class="ev-keys">${pairs.map(([name, cls]) => `<span class="ev-key"><b class="${cls}"></b>${esc(name)}</span>`).join('')}</div>` : '';

/* The number a card leads with, and the line beside it that compares. */
const trHeadline = (big, sub, cls) => `<div class="tr-headline"><span class="tr-big">${esc(big)}</span>${sub ? `<span class="tr-sub ${cls || ''}">${esc(sub)}</span>` : ''}</div>`;

/* A card with a chart in it. The holder is measured after it is on the page,
   so the chart is drawn at the width it has. */
function trCard(title, note, keys, headline, link) {
  // A link wraps the plot when a click somewhere on it should open a page: the written chart opens Ingest.
  return `<section class="card tr-card"><div class="head"><h2>${esc(title)}</h2><span class="faint">${esc(note)}</span></div>${headline || ''}${link ? `<a class="tr-plot-link" href="${esc(link)}" title="Open Ingest">` : ''}<div class="tr-plot"></div>${link ? '</a>' : ''}${trKeys(keys)}</section>`;
}

function trDraw() {
  TR.drawn = TR.drawn.filter((c) => c.el.isConnected);
  TR.drawn.forEach((c) => { const plot = c.el.querySelector('.tr-plot'); if (plot && plot.clientWidth) plot.innerHTML = c.lines ? trLines(plot.clientWidth, c.days, c.series, c.label) : trBars(plot.clientWidth, c.days, c.series, c.stacked, c.label, c.overlay); });
}

/* Put chart cards into a holder. charts: [{ title, note, keys, headline, days, series, stacked, overlay, label }], or a string of HTML for a card that is not a chart. */
function trFill(holder, charts) {
  holder.innerHTML = charts.map((c) => typeof c === 'string' ? c : trCard(c.title, c.note, c.keys || [], c.headline, c.link)).join('');
  [...holder.children].forEach((el, n) => { if (typeof charts[n] !== 'string') TR.drawn.push({ el, ...charts[n] }); });
  trDraw();
}

const trSum = (values) => values.reduce((a, b) => a + (b || 0), 0);
const trPeak = (days, values) => { const most = Math.max(...values); return most ? `${fmtNum(most)} on ${trDay(days[values.indexOf(most)])}` : 'none'; };
/* This week against the week before, in words: the last seven days of the
   window against the seven before them. Nothing to say with fewer than eight. */
function trWeekOn(values, word) {
  if (values.length < 8) return '';
  const now = trSum(values.slice(-7)), before = trSum(values.slice(-14, -7));
  if (!before) return now ? `${fmtNum(now)} ${word} this week, none the week before` : '';
  const pct = Math.round(100 * (now - before) / before);
  return pct === 0 ? 'level with the week before' : `${pct > 0 ? '+' : ''}${pct}% on the week before`;
}
const trTone = (delta, badWhenUp) => delta > 0 ? (badWhenUp ? 'tr-sub-bad' : 'tr-sub-ok') : delta < 0 ? (badWhenUp ? 'tr-sub-ok' : 'tr-sub-bad') : '';

/* ------------------------------------------------------------ overview */
/* d: the counts a day from the access log, or null when there are none to
   draw. d.denied, the searches a collection's policy turned away, is there
   for whoever may read the log and drawn as a line over the searches.
   g: what was written across every collection, or null. opts.ingestLink:
   where a click on the written chart goes, for somebody who may add
   documents. Nothing here is about who signed in: that is the Access page. */
function trOverview(holder, d, g, opts = {}) {
  const cards = [];
  if (d) {
    const n = d.days.length, all = trSum(d.searches);
    const searchesOn = trWeekOn(d.searches, 'searches');
    const denied = d.denied ? trSum(d.denied) : 0;
    const tone = denied ? 'tr-sub-bad' : trTone(searchesOn.startsWith('-') ? -1 : searchesOn.startsWith('+') ? 1 : 0);
    cards.push({ title: 'Searches a day', note: `last ${n} days`, days: d.days, stacked: false,
      headline: trHeadline(fmtNum(all), [searchesOn, denied ? `${fmtNum(denied)} refused by a policy` : ''].filter(Boolean).join(' · '), tone),
      keys: d.denied ? [['Searches', 'tr-acc'], ['Refused by a policy', 'tr-bad']] : [],
      series: [{ cls: 'tr-acc', values: d.searches }], overlay: denied ? { cls: 'tr-bad', values: d.denied, word: 'refused by a policy' } : null,
      label: `Searches a day for the last ${n} days, ${trPeak(d.days, d.searches)} at the most${denied ? `, with the searches a policy refused as a line, ${trPeak(d.days, d.denied)} at the most` : ''}` });
    // Search time is there once a search has been timed. A log written before
    // searches were timed has none, and an empty chart would say nothing.
    const medians = (d.search_ms_median || []).filter((v) => v !== null && v !== undefined);
    const slowest = (d.search_ms_p95 || []).filter((v) => v !== null && v !== undefined);
    const middle = medians.length ? [...medians].sort((a, b) => a - b)[Math.floor(medians.length / 2)] : null;
    const spread = medians.length ? Math.max(...medians) - Math.min(...medians) : 0;
    const time = medians.length ? [{ title: 'Search time', note: 'median · slowest 1 in 20', days: d.days, lines: true,
      headline: trHeadline(`${fmtNum(Math.round(middle))} ms`, spread <= Math.max(10, middle * 0.25) ? 'median, steady all fortnight' : `median, between ${fmtNum(Math.round(Math.min(...medians)))} and ${fmtNum(Math.round(Math.max(...medians)))} ms`),
      series: [{ cls: 'tr-mute', values: d.search_ms_p95, dashed: true, unit: ' ms' }, { cls: 'tr-acc', values: d.search_ms_median, area: true, unit: ' ms' }],
      label: `Search time a day for the last ${n} days: the median is about ${fmtNum(Math.round(middle))} milliseconds, and the slowest one in twenty peaks at ${fmtNum(Math.round(Math.max(...slowest)))}` }] : [];
    cards.push(...time);
  }
  if (g) {
    const n = g.days.length, all = trSum(g.written), lows = g.low || g.days.map(() => 0), low = trSum(lows);
    const good = g.written.map((v, i) => Math.max(0, (v || 0) - (lows[i] || 0)));
    const pct = all ? Math.round(100 * low / all) : 0;
    cards.push({ title: 'Written a day', note: `last ${n} days · every collection`, days: g.days, stacked: true, link: opts.ingestLink || null,
      headline: trHeadline(fmtNum(all), all ? (low ? `${pct < 1 ? 'under 1' : pct}% below the quality line` : 'all above the quality line') : 'none in these days', low ? 'tr-sub-bad' : all ? 'tr-sub-ok' : ''),
      keys: [['Written', 'tr-acc'], ['Below the quality line', 'tr-bad']],
      series: [{ cls: 'tr-bad', values: lows, front: true }, { cls: 'tr-acc', values: good, back: true }],
      label: `Chunks written a day across every collection for the last ${n} days, ${trPeak(g.days, g.written)} at the most, with the part below the quality line in red` });
  }
  trFill(holder, cards);
}

/* --------------------------------------------------------------- audit */
/* One row a collection: name, a track split by outcome, the total, and the
   share turned away. Busiest first, ten a page, with a find. The rows are
   the whole list the audit route counted, one small row a collection, so
   the page turns here without asking again. */
function trByCollectionRows() {
  const total = (r) => r.allowed + r.denied + r.refused;
  const q = TR.byc.q.trim().toLowerCase();
  const rows = [...TR.byc.rows].sort((a, b) => total(b) - total(a)).filter((r) => !q || String(r.collection).toLowerCase().includes(q));
  const pages = Math.max(1, Math.ceil(rows.length / TR_PAGE));
  TR.byc.page = Math.min(Math.max(0, TR.byc.page), pages - 1);
  const from = TR.byc.page * TR_PAGE, shown = rows.slice(from, from + TR_PAGE);
  const most = Math.max(1, ...rows.map(total));
  const share = (part, r) => (100 * part / Math.max(total(r), 1)).toFixed(2);
  const away = (r) => Math.round(100 * (r.denied + r.refused) / Math.max(total(r), 1));
  const list = shown.map((r) => `<div class="tr-row"><span class="mono">${esc(r.collection)}</span><span class="tr-track" style="width: ${(100 * total(r) / most).toFixed(1)}%">${r.allowed ? `<i class="tr-ok tr-back" style="width: ${share(r.allowed, r)}%"></i>` : ''}${r.denied ? `<i class="tr-warn" style="width: ${share(r.denied, r)}%"></i>` : ''}${r.refused ? `<i class="tr-bad" style="width: ${share(r.refused, r)}%"></i>` : ''}</span><span class="tr-n">${fmtNum(total(r))}</span><span class="tr-say${away(r) >= 10 ? ' warn' : ''}">${away(r) ? `${away(r)}% turned away` : 'none turned away'}</span></div>`).join('');
  const bad = rows.filter((r) => away(r) >= 10).length;
  return `${pager('byc', { placeholder: 'Find a collection', q: TR.byc.q, total: rows.length, from, count: shown.length })}<div class="tr-rows">${list || `<div class="muted">${q ? 'No collection is called that.' : 'No decisions in these days.'}</div>`}</div><div class="tr-foot"><span class="faint">${rows.length ? (bad ? `${bad} of these ${fmtNum(rows.length)} turned away one in ten or more.` : 'None of these turned away one in ten.') : 'Decisions only. A collection with no policy makes none.'}</span>${trKeys([['Allowed', 'tr-ok'], ['Denied', 'tr-warn'], ['Refused', 'tr-bad']])}</div>`;
}
function trByCollection(rows, n) {
  TR.byc = { rows: rows || [], q: '', page: 0 };
  return `<section class="card tr-card" id="tr-byc"><div class="head"><h2>By collection</h2><span class="faint">last ${n} days · busiest first · decisions only</span></div>${trByCollectionRows()}</section>`;
}
function trByCollectionRedraw() { const card = $('tr-byc'); if (!card) return; const head = card.querySelector('.head'); card.innerHTML = ''; card.appendChild(head); card.insertAdjacentHTML('beforeend', trByCollectionRows()); }
PAGERS.byc = (q, page) => { TR.byc.q = q; TR.byc.page = page; trByCollectionRedraw(); };

function trAudit(holder, daily, byCollection) {
  const n = daily.days.length;
  const whole = daily.days.map((_, d) => daily.allowed[d] + daily.denied[d] + daily.refused[d]);
  const away = daily.days.map((_, d) => daily.denied[d] + daily.refused[d]);
  const awayOn = trWeekOn(away, 'turned away');
  trFill(holder, [
    { title: 'Outcomes a day', note: `last ${n} days`, days: daily.days, stacked: true,
      headline: trHeadline(fmtNum(trSum(whole)), trSum(away) ? `${fmtNum(trSum(away))} turned away${awayOn ? `, ${awayOn}` : ''}` : 'none turned away', trSum(away) ? trTone(awayOn.startsWith('+') ? 1 : awayOn.startsWith('-') ? -1 : 0, true) : ''),
      keys: [['Allowed', 'tr-ok'], ['Denied', 'tr-warn'], ['Refused or undecidable', 'tr-bad']],
      series: [{ cls: 'tr-ok', values: daily.allowed }, { cls: 'tr-warn', values: daily.denied }, { cls: 'tr-bad', values: daily.refused }],
      label: `Decisions a day for the last ${n} days by outcome, ${trPeak(daily.days, whole)} at the most; denied peaks at ${trPeak(daily.days, daily.denied)}` },
    trByCollection(byCollection, n),
  ]);
}

/* -------------------------------------------------------------- access */
const TR_IDLE_DAYS = 30;

/* readers: the page the server sent, [{ who, key, searches, reads }], the
   busiest first, with total and offset; keys: the server's list of keys, or
   null for somebody who may not see them. */
/* Sign-ins a day with refusals as a line: who came in and who was turned
   away is about people, so it sits on the Access page, not the Overview. */
function trSigninsCard(d) {
  const n = d.days.length;
  const refused = trSum(d.refused), refusedBefore = trSum(d.refused.slice(-14, -7)), refusedNow = trSum(d.refused.slice(-7));
  const refusedLine = refused ? `${fmtNum(refused)} refused${d.refused.length >= 8 && refusedNow !== refusedBefore ? `, ${fmtNum(Math.abs(refusedNow - refusedBefore))} ${refusedNow > refusedBefore ? 'more' : 'fewer'} than the week before` : ''}` : 'none refused';
  return { title: 'Sign-ins and refusals', note: `last ${n} days`, days: d.days, stacked: false,
    headline: trHeadline(fmtNum(trSum(d.signins)), refusedLine, refused ? 'tr-sub-bad' : ''),
    keys: [['Signed in', 'tr-mute'], ['Refused', 'tr-bad']],
    series: [{ cls: 'tr-mute', values: d.signins }], overlay: { cls: 'tr-bad', values: d.refused, word: 'refused' },
    label: `Sign-ins a day for the last ${n} days, with refusals as a line; refusals peak at ${trPeak(d.days, d.refused)}` };
}
const trIdleFor = (k) => k.last_used ? Math.floor((Date.now() / 1000 - k.last_used) / 86400) : Infinity;
const trUsed = (k) => trIdleFor(k) >= TR_IDLE_DAYS
  ? `<span class="pill warn" title="Not used for ${TR_IDLE_DAYS} days or more. A key nobody uses is a key to revoke.">${k.last_used ? `${trIdleFor(k)}d ago` : 'never'}</span>`
  : `<span class="muted">${esc(agoTs(k.last_used))}</span>`;
/* The live keys by last use, ten a page. */
function trKeysCard() {
  const k = TR.keys;
  const pages = Math.max(1, Math.ceil(k.live.length / TR_PAGE)); k.page = Math.min(Math.max(0, k.page), pages - 1);
  const from = k.page * TR_PAGE, shown = k.live.slice(from, from + TR_PAGE);
  const list = shown.map((key) => `<div class="tr-kr"><span class="mono">${esc(key.name)}</span><span class="muted">${esc(key.role)}</span><span class="tr-end">${trUsed(key)}</span></div>`).join('');
  return `<section class="card tr-card" id="tr-keys"><div class="head"><h2>Keys, by last use</h2><span class="faint">${k.live.length ? `${k.idle} of ${k.live.length} idle` : ''}</span></div>${k.live.length ? `${pager('keys', { find: false, total: k.live.length, from, count: shown.length })}<div class="tr-list">${list}</div>` : '<div class="muted">No keys have been made.</div>'}</section>`;
}
PAGERS.keys = (q, page) => { if (!TR.keys) return; TR.keys.page = page; const card = $('tr-keys'); if (card) card.outerHTML = trKeysCard(); };

function trAccess(holder, readers, days, keys, paging, daily) {
  const total = (r) => r.searches + r.reads, most = Math.max(1, ...readers.map(total));
  const name = (r) => r.key ? `${r.who.replace(/^key:/, '')} (key)` : r.who;
  const rows = readers.map((r) => `<div class="tr-row wide"><span class="mono">${esc(name(r))}</span><span class="tr-track" style="width: ${(100 * total(r) / most).toFixed(1)}%"><i class="tr-acc" style="width: ${(100 * r.searches / Math.max(total(r), 1)).toFixed(2)}%"></i><i class="tr-mute" style="width: ${(100 * r.reads / Math.max(total(r), 1)).toFixed(2)}%"></i></span><span class="tr-n">${fmtNum(total(r))}</span></div>`).join('');
  const bar = paging ? pager('readers', { placeholder: 'Find a person or key', q: paging.q, total: paging.total, from: paging.offset, count: readers.length }) : '';
  const who = `<section class="card tr-card"><div class="head"><h2>Who reads most</h2><span class="faint">last ${days} days · busiest first</span></div>${bar}${readers.length ? `<div class="tr-rows">${rows}</div>${trKeys([['Searches', 'tr-acc'], ['Text read', 'tr-mute']])}` : `<div class="muted">${paging && paging.q ? 'Nobody is called that.' : 'Nobody searched or read in these days.'}</div>`}</section>`;
  const cards = daily && daily.signins ? [trSigninsCard(daily), who] : [who];
  if (keys) {
    const live = keys.filter((k) => !k.revoked_at && !k.expired).sort((a, b) => (b.last_used || 0) - (a.last_used || 0));
    TR.keys = { live, idle: live.filter((k) => trIdleFor(k) >= TR_IDLE_DAYS).length, page: TR.keys ? TR.keys.page : 0 };
    cards.push(trKeysCard());
  }
  trFill(holder, cards);
}

/* ---------------------------------------------------------- a collection */
function trGrowth(holder, d) {
  const n = d.days.length, all = trSum(d.written);
  const on = trWeekOn(d.written, 'chunks');
  trFill(holder, [{ title: 'Chunks written a day', note: `last ${n} days`,
    headline: trHeadline(fmtNum(all), all ? `${on || `most on ${trDay(d.days[d.written.indexOf(Math.max(...d.written))])}`}` : 'none in these days', trTone(on.startsWith('+') ? 1 : on.startsWith('-') ? -1 : 0)),
    days: d.days, stacked: false, series: [{ cls: 'tr-acc', values: d.written }],
    label: `Chunks written a day for the last ${n} days, ${trPeak(d.days, d.written)} at the most` }]);
  holder.querySelector('.tr-card').insertAdjacentHTML('beforeend', '<div class="faint">Counted from the chunks that are here now. What was written and since deleted is not in it.</div>');
}

/* builds: the server's list. One row a build, oldest first, by when each
   first wrote: its id, a bar for its chunks, the count, and its quality,
   in red when it reads badly. The current build is marked. */
function trBuilds(holder, builds, line) {
  const known = builds.filter((b) => b.written_at).sort((a, b) => a.written_at < b.written_at ? -1 : 1);
  if (known.length < 2) { holder.innerHTML = ''; return; }
  TR.builds = { known, line, page: 0 };
  holder.innerHTML = trBuildsCard();
}
function trBuildsCard() {
  const { known, line } = TR.builds;
  const bad = (b) => b.quality !== null && b.quality !== undefined && b.quality < line;
  const badly = known.filter(bad).length;
  const most = Math.max(1, ...known.map((b) => b.chunks));
  const pages = Math.max(1, Math.ceil(known.length / TR_PAGE)); TR.builds.page = Math.min(Math.max(0, TR.builds.page), pages - 1);
  const from = TR.builds.page * TR_PAGE, shown = known.slice(from, from + TR_PAGE);
  const rows = shown.map((b) => `<div class="tr-row build"><span class="row" style="gap: 8px">${idChip(b.build_id)}${b.current ? pill('current', 'acc') : ''}</span><span class="tr-track plain"><i class="${bad(b) ? 'tr-bad' : b.current ? 'tr-acc' : 'tr-mute tr-back'}" style="width: ${(100 * b.chunks / most).toFixed(1)}%"></i></span><span class="tr-n">${fmtNum(b.chunks)}</span><span class="tr-say${bad(b) ? ' bad' : ''}">${b.quality !== null && b.quality !== undefined ? `quality ${Number(b.quality).toFixed(2)}` : 'not scored'}</span></div>`).join('');
  return `<section class="card tr-card" id="tr-builds"><div class="head"><h2>Chunks by build</h2><span class="faint">oldest first · ${badly ? `${badly} build${badly === 1 ? ' reads' : 's read'} badly, under ${line}` : 'every build reads well'}</span></div>${pager('bbuilds', { find: false, total: known.length, from, count: shown.length })}<div class="tr-rows">${rows}</div>${trKeys([['Current build', 'tr-acc'], ['Earlier builds', 'tr-mute'], ['Reads badly', 'tr-bad']])}</section>`;
}
PAGERS.bbuilds = (q, page) => { if (!TR.builds) return; TR.builds.page = page; const card = $('tr-builds'); if (card) card.outerHTML = trBuildsCard(); };

/* --------------------------------------------------------------- ingest */
/* docs: [{ name, quality }], the documents of this run, in the order sent. */
function trBatch(docs, line) {
  if (docs.length < 2) return '';
  TR.batch = { docs, line, page: TR.batch && TR.batch.docs === docs ? TR.batch.page : 0 };
  return trBatchHtml();
}
function trBatchHtml() {
  const { docs, line } = TR.batch;
  const low = docs.filter((d) => d.quality < line).length;
  const pages = Math.max(1, Math.ceil(docs.length / TR_PAGE)); TR.batch.page = Math.min(Math.max(0, TR.batch.page), pages - 1);
  const from = TR.batch.page * TR_PAGE, shown = docs.slice(from, from + TR_PAGE);
  const rows = shown.map((d) => `<div class="tr-qb"><span class="mono">${esc(d.name)}</span><span class="tr-q"><i class="${d.quality < line ? 'tr-warn' : 'tr-ok'}" style="width: ${(100 * Math.max(0, Math.min(1, d.quality))).toFixed(0)}%"></i><b style="left: ${(100 * line).toFixed(0)}%"></b></span><span class="tr-n${d.quality < line ? ' low' : ''}">${d.quality.toFixed(2)}</span></div>`).join('');
  return `<div class="tr-batch" id="tr-batch"><div class="tr-batch-head"><span>Quality of this run</span><span class="faint">${countOf(docs.length, 'document')} · ${low ? `${low} under the line at ${line}` : `all over the line at ${line}`}</span></div>${pager('batch', { find: false, total: docs.length, from, count: shown.length })}<div class="tr-rows">${rows}</div></div>`;
}
PAGERS.batch = (q, page) => { if (!TR.batch) return; TR.batch.page = page; const el = $('tr-batch'); if (el) el.outerHTML = trBatchHtml(); };

window.addEventListener('resize', () => { clearTimeout(TR.timer); TR.timer = setTimeout(trDraw, 150); });
