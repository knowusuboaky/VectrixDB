/* The build itself: what Vite put in dist, held to what a browser will ask for.
 *
 * The Python functional test serves the build and reads it back. This one is
 * about the folder: every file the page names is in it, nothing it names is
 * fetched from somewhere we did not choose, and the copied pages went in
 * whole. Skipped, loudly, until there is a build.
 *
 * Author: Kwadwo Daddy Nyame Owusu - Boakye
 */

import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'

const DASHBOARD = path.resolve(import.meta.dirname, '../../..')
const DIST = path.join(DASHBOARD, 'Frontend', 'dist')
const PAGES = path.join(DASHBOARD, 'Frontend', 'public', 'pages')

const built = existsSync(path.join(DIST, 'index.html'))
const options = { skip: built ? false : `no build in ${DIST}. Run: cd Frontend && python run_prod.py` }
const page = built ? readFileSync(path.join(DIST, 'index.html'), 'utf8') : ''

/** The hosts the page is allowed to reach out to, and why. */
const ALLOWED = [
  'https://fonts.googleapis.com', // the two IBM Plex faces
  'https://fonts.gstatic.com',
  'https://cdnjs.cloudflare.com', // cytoscape, for the provenance graph
]

/** Paths the Backend forwards to the retrieval service, so no file of ours answers them. */
const FORWARDED = ['/api', '/auth', '/health', '/docs', '/openapi.json', '/brand.json', '/brand.css', '/brand']
const forwarded = (href) => FORWARDED.some((prefix) => href === prefix || href.startsWith(prefix + '/'))

test('every file the page names is in the build', options, () => {
  const named = [...new Set([...page.matchAll(/(?:src|href)="(\/[^"]+)"/g)].map((found) => found[1]))]
  assert.ok(named.length > 0, 'the page names no files at all')
  for (const href of named.filter((one) => !forwarded(one))) {
    assert.ok(existsSync(path.join(DIST, href.slice(1))), `the page names ${href} and the build has no such file`)
  }
  // The API reference is the service's, and the Backend has to be the one that
  // hands it over: a link to it with nothing forwarding it is a dead menu item.
  for (const href of named.filter(forwarded)) {
    assert.ok(FORWARDED.some((prefix) => href.startsWith(prefix)), `${href} is named and nothing forwards it`)
  }
})

test('nothing is fetched from a host we did not choose', options, () => {
  const outside = [...new Set([...page.matchAll(/(?:src|href)="(https?:\/\/[^"/]+)/g)].map((found) => found[1]))]
  for (const host of outside) {
    assert.ok(ALLOWED.includes(host), `the page fetches from ${host}, which is not one of ours`)
  }
})

test('the copied pages went into the build whole', options, () => {
  for (const file of readdirSync(PAGES)) {
    assert.deepEqual(
      readFileSync(path.join(DIST, 'pages', file)),
      readFileSync(path.join(PAGES, file)),
      `dist/pages/${file} is not what public/pages/${file} holds`,
    )
  }
})

test('the build has one hashed module of its own', options, () => {
  const assets = readdirSync(path.join(DIST, 'assets')).filter((name) => name.endsWith('.js'))
  assert.equal(assets.length, 1, `expected one module in dist/assets, found ${assets.join(', ') || 'none'}`)
  assert.match(assets[0], /^index-[A-Za-z0-9_-]+\.js$/, 'the module carries no hash, so it cannot be cached')
})
