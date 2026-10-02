/* The app's own page script: the flows that use the function app's routes.
 *
 * setup.js adds its names to the dispatcher's allowlist at run time, so the
 * parse test's static reading of ACTIONS does not see them: this one does.
 * It also holds the script to the app's routes, and to the policy editor
 * being the library's, defined once in app.js.
 *
 * Author: Kwadwo Daddy Nyame Owusu - Boakye
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'

const PAGES = path.resolve(import.meta.dirname, '../../../../../Frontend/public/pages')
const setup = readFileSync(path.join(PAGES, 'setup.js'), 'utf8')
const app = readFileSync(path.join(PAGES, 'app.js'), 'utf8')
const html = readFileSync(path.resolve(PAGES, '../../index.html'), 'utf8')

const defined = (source) => new Set([...source.matchAll(/^(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(/gm)].map((found) => found[1]))

test('every name setup.js lets the page dispatch is a function it defines', () => {
  const added = setup.match(/^\[([^\]]+)\]\.forEach\(\(name\) => ACTIONS\.add\(name\)\)/m)
  assert.ok(added, 'setup.js no longer adds its names to ACTIONS')
  const names = [...added[1].matchAll(/'([^']+)'/g)].map((found) => found[1])
  const own = defined(setup)
  assert.deepEqual(names.filter((name) => !own.has(name)), [])
  // Everything the page's markup dispatches from the app's own dialogs is one of them.
  for (const name of ['openCreate', 'pickCcMode', 'createCollection', 'uploadFiles', 'deleteEverything']) {
    assert.ok(html.includes(`"${name}"`), `${name} is not on the page`)
    assert.ok(names.includes(name), `${name} is not added to ACTIONS`)
  }
})

test('it speaks to the app\'s routes, and the policy editor is the library\'s', () => {
  for (const route of ['/setup`', '/files`', '/setup/status`', '/delete`']) assert.ok(setup.includes(route), route)
  assert.equal(setup.includes('/api/v2/collections'), false, 'the library\'s create route makes a collection with no policy')
  for (const name of ['newWho', 'policyFrom', 'renderWho', 'describePolicy']) {
    assert.ok(setup.includes(`${name}(`), `${name} is not used`)
    assert.ok(defined(app).has(name), `${name} is not the library\'s`)
    assert.equal(defined(setup).has(name), false, `${name} is defined twice`)
  }
})

test('a collection is made with its policy first, and deleting keeps no copy', () => {
  const create = setup.slice(setup.indexOf('async function createCollection'), setup.indexOf('/* ------------------------------------------------------------ add files */'))
  assert.ok(create.indexOf('/setup`') < create.indexOf('/files`'), 'the record and policy go first, then the files')
  assert.ok(create.includes('policyFrom(WHO.cc)'), 'the policy is required')
  const del = setup.slice(setup.indexOf('async function deleteEverything'))
  assert.ok(del.includes("method: 'POST' }") && del.includes('/delete`'))
  assert.equal(/copy|aside|archive/i.test(del.replace('Nothing of it is left', '')), false, 'nothing is kept')
})
