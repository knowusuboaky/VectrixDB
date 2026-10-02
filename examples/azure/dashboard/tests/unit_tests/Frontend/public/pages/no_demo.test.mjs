/* No demo data, anywhere.
 *
 * This dashboard is built against a service that holds nothing until a file is
 * ingested, so the pages must never offer to seed themselves. Every page then
 * has to stand on its own empty state, which is what a company's first install
 * actually looks like.
 *
 * app.css is left out on purpose: it still carries the .demo-strip rules,
 * because it is copied from the library byte for byte and unused CSS is
 * cheaper than a file that has to be re-checked on every library change.
 *
 * Author: Kwadwo Daddy Nyame Owusu - Boakye
 */

import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'

const FRONTEND = path.resolve(import.meta.dirname, '../../../../../Frontend')
const PAGES = path.join(FRONTEND, 'public', 'pages')

const read = (file) => readFileSync(path.join(PAGES, file), 'utf8')

test('the demo data file did not come across', () => {
  assert.equal(existsSync(path.join(PAGES, 'demo-data.js')), false)
  assert.deepEqual(
    readdirSync(PAGES).sort(),
    ['app.css', 'app.js', 'chunking.js', 'evaluate.js', 'setup.css', 'setup.js', 'sso-boot.js', 'theme.js', 'trends.js'],
  )
})

test('nothing in the scripts or the page mentions a demo', () => {
  for (const file of ['app.js', 'setup.js', 'setup.css', 'theme.js', 'sso-boot.js', 'trends.js', 'evaluate.js', 'chunking.js']) {
    assert.equal(/demo/i.test(read(file)), false, `${file} still mentions a demo`)
  }
  assert.equal(/demo/i.test(readFileSync(path.join(FRONTEND, 'index.html'), 'utf8')), false)
})

test('the loader is gone and the action cannot be dispatched', () => {
  const app = read('app.js')
  assert.equal(app.includes('function loadDemo'), false)
  assert.equal(app.includes('DEMO_DATA'), false)
  // The dispatcher runs a name only when ACTIONS holds it, so a leftover
  // button anywhere would be inert as well as absent.
  const actions = app.slice(app.indexOf('const ACTIONS'), app.indexOf('\n', app.indexOf('const ACTIONS')))
  assert.equal(actions.includes('loadDemo'), false)
  assert.ok(actions.includes('createCollection'), 'the rest of the actions are still there')
})

test('the empty states say what to do instead', () => {
  const app = read('app.js')
  assert.ok(app.includes('Create a collection, then add a document from Ingest.'))
  assert.ok(app.includes('<div>Create one, then add a document from Ingest.</div>'))
})
