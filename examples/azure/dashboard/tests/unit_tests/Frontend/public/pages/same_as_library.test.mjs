/* The copy is the library's dashboard, and the edits to it are the four we chose.
 *
 * Six files and the favicon must be byte for byte the library's, so nothing
 * drifts by accident. index.html and app.js are edited, and this holds the
 * edits to a list: every line ours has that the library's does not must be one
 * we meant, and every edit we meant must be there. When the library's pages
 * change, as they did when the Chunking tab landed, this is what tells us.
 *
 * Author: Kwadwo Daddy Nyame Owusu - Boakye
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'

const DASHBOARD = path.resolve(import.meta.dirname, '../../../../..')
const FRONTEND = path.join(DASHBOARD, 'Frontend')
const LIBRARY = path.resolve(DASHBOARD, '../../../vectrixdb/dashboard')

const VERBATIM = ['app.css', 'theme.js', 'sso-boot.js', 'trends.js', 'evaluate.js', 'chunking.js']

/** Lines ours has that the library's does not, trimmed, blanks left out. */
function added(ours, theirs) {
  const had = new Set(theirs.split('\n'))
  return ours
    .split('\n')
    .filter((line) => !had.has(line))
    .map((line) => line.trim())
    .filter(Boolean)
}

/** The lines between the app's own markers: dialogs and a page that use the app's routes. */
function ownFlows(source) {
  const own = new Set()
  const open = /<!-- =+ the app's own flows -->/g
  let found
  while ((found = open.exec(source))) {
    const end = source.indexOf('\n', source.indexOf("end of the app's own flows", found.index))
    for (const line of source.slice(found.index, end).split('\n')) own.add(line.trim())
  }
  return own
}

/** Every added line is one of these, and each of these is there. */
function heldTo(lines, edits, where) {
  for (const line of lines) {
    const matched = edits.filter(([, matches]) => matches(line))
    assert.equal(matched.length > 0, true, `${where} changed a line nobody asked for:\n${line}`)
  }
  for (const [name, matches] of edits) {
    assert.equal(lines.some(matches), true, `${where} is missing the edit: ${name}`)
  }
}

test('every file we did not edit is the library’s, byte for byte', () => {
  for (const file of VERBATIM) {
    assert.deepEqual(
      readFileSync(path.join(FRONTEND, 'public', 'pages', file)),
      readFileSync(path.join(LIBRARY, file)),
      `${file} differs from the library’s`,
    )
  }
  assert.deepEqual(
    readFileSync(path.join(FRONTEND, 'public', 'favicon.svg')),
    readFileSync(path.join(LIBRARY, 'favicon.svg')),
  )
})

test('app.js differs only where we meant it to', () => {
  const ours = readFileSync(path.join(FRONTEND, 'public', 'pages', 'app.js'), 'utf8')
  const theirs = readFileSync(path.join(LIBRARY, 'app.js'), 'utf8')

  heldTo(
    added(ours, theirs),
    [
      // Served at the root of its own server, not under /dashboard/, where a
      // trailing slash would make every call //api/v1/....
      ['the API base', (line) => line.startsWith('const API = location.origin') || line.startsWith('.replace(/')],
      // The Overview's collections card, without the demo button beside it.
      ['the overview actions', (line) => line.includes("$('ov-col-actions').hidden") && !line.includes('ov-demo')],
      // What to do on a server that holds nothing.
      ['the overview empty state', (line) => line.includes('Nothing is indexed yet') || line.includes('Enter it to create a collection')],
      ['the collections empty state', (line) => line.includes('No collections yet') && line.includes('add a document from Ingest')],
      // Neither of these prefers a collection that no longer exists.
      ['trySearch', (line) => line.startsWith('function trySearch(') && !line.includes("'demo'")],
      ['the console preset', (line) => line.includes("|| 'your-collection';")],
      // The dispatcher's list, without the loader's name in it.
      ['the actions list', (line) => line.startsWith('const ACTIONS') && !line.includes('loadDemo')],
      // Learn is the library's teaching page; a company's dashboard has no lessons in it.
      ['the pages without Learn', (line) => line.startsWith('const PAGES') && !line.includes('learn')],
      ['the titles without Learn', (line) => line.startsWith('const TITLES') && !line.includes('learn')],
      ['the routes without Learn', (line) => line.startsWith('#/evaluate/retrieval') && !line.includes('learn')],
      // The app's own: a setup page at #/collections/<name>/setup, where a new collection's spinner is.
      ['the setup route', (line) => line.includes("parts[2] === 'setup' ? 'setup' : 'collection'")],
      ['the setup page under Collections', (line) => line.includes("page === 'collection' || page === 'setup'")],
      ['the setup loader', (line) => line.includes("page === 'setup') await loadSetup()")],
      // Its dialogs open through the app's own openers, which reset them.
      ['the create dialog opener', (line) => line.includes('openCreate') && !line.includes('dlg-create')],
      ['the add files button', (line) => line.includes("['openFiles']") && line.includes('chead-act')],
      ['the policies a collection page needs', (line) => line.includes('if (state.signinOn && !state.policies) await loadPolicies();')],
      // The Search page has one tab: the Graph tab is not part of a company's dashboard.
      ['the tabs without Graph', (line) => line.startsWith("['results'].forEach(")],
      // The files that would not read: the app's own card on Ingest, its count on the Overview.
      ['the poison card after the document index', (line) => line.includes("if (typeof loadPoison === 'function') await loadPoison();")],
      ['the poison count in what needs attention', (line) => line.includes("if (typeof poisonAttention === 'function') out.push(...poisonAttention());")],
      ['the poison count loaded with the overview', (line) => line.includes("typeof loadPoisonCount === 'function' ? loadPoisonCount() : null")],
    ],
    'app.js',
  )
  for (const gone of ['async function loadGraph(', 'async function extractGraph(', 'function ensureCytoscape(', 'cytoscape']) {
    assert.equal(ours.includes(gone), false, `${gone} draws the knowledge graph, which the app does not carry`)
  }
  for (const gone of ['async function createCollection() {', 'function confirmDelete(name) {']) {
    assert.equal(ours.includes(gone), false, `${gone} posts to the library's routes; the app's own is in setup.js`)
  }
  for (const gone of ['function showLearn(', 'function gotoGuide(', "'learn'"]) {
    assert.equal(ours.includes(gone), false, `${gone} belongs to Learn, which the app does not have`)
  }
  assert.ok(ours.length < theirs.length, 'ours is the shorter file: the loader is gone')
})

test('index.html differs only where we meant it to', () => {
  const ours = readFileSync(path.join(FRONTEND, 'index.html'), 'utf8')
  const theirs = readFileSync(path.join(LIBRARY, 'index.html'), 'utf8')

  const own = ownFlows(ours)
  heldTo(
    added(ours, theirs),
    [
      ['the favicon path', (line) => line === '<link rel="icon" href="/favicon.svg">'],
      ['the stylesheet path', (line) => line === '<link rel="stylesheet" href="/pages/app.css">'],
      ['the app\'s own stylesheet', (line) => line === '<link rel="stylesheet" href="/pages/setup.css">'],
      ['the brand\'s colours, from the retrieval service', (line) => line === '<link rel="stylesheet" href="/brand.css">'],
      ['the app\'s own flows', (line) => own.has(line)],
      ['the create dialog opener', (line) => line.includes('[["openCreate"]]')],
      ['the theme script', (line) => line === '<script src="/pages/theme.js"></script>'],
      ['the sign-in boot script', (line) => line === '<script src="/pages/sso-boot.js"></script>'],
      ['the brand as data', (line) => line.includes('id="vx-brand-data"') || line.startsWith('<!-- The brand is data')],
      ['the page scripts', (line) => /^<script src="\/pages\/(app|setup|trends|evaluate|chunking)\.js"><\/script>$/.test(line)],
      ['our own module', (line) => line === '<script type="module" src="/src/main.js"></script>'],
      ['the empty state without the demo button or the guide', (line) => line.includes('Create collection') && !line.includes('guide')],
    ],
    'index.html',
  )
  for (const id of ['dlg-create', 'dlg-files', 'dlg-delete', 'page-setup']) {
    assert.ok([...own].some((line) => line.includes(`id="${id}"`)), `${id} is not inside the app's own markers`)
  }
  for (const gone of ['data-page="learn"', 'id="page-learn"', '#/learn']) {
    assert.equal(ours.includes(gone), false, `${gone} is Learn, which a company's dashboard does not carry`)
  }
  for (const gone of ['id="tab-graph"', 'id="tabpanel-graph"', 'data-col-filter="graph"', 'cytoscape']) {
    assert.equal(ours.includes(gone), false, `${gone} is the Graph tab, which a company's dashboard does not carry`)
  }
})
