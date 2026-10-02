/* The copied pages still parse, and every handler the page dispatches is defined.
 *
 * Taking the demo out of app.js left the loader's closing brace behind, and a
 * page script that does not parse loads nothing at all: no error on the page,
 * just an application that never starts. So each file is checked the way node
 * checks one, and the dispatcher's allowlist is held to what the file defines,
 * because a name in the list with no function behind it is a button that does
 * nothing when it is pressed.
 *
 * Author: Kwadwo Daddy Nyame Owusu - Boakye
 */

import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'

const PAGES = path.resolve(import.meta.dirname, '../../../../../Frontend/public/pages')
const SCRIPTS = readdirSync(PAGES).filter((name) => name.endsWith('.js'))

test('every page script parses', () => {
  assert.ok(SCRIPTS.length >= 6, `expected the library’s six page scripts, found ${SCRIPTS.length}`)
  for (const name of SCRIPTS) {
    assert.doesNotThrow(
      () => execFileSync(process.execPath, ['--check', path.join(PAGES, name)], { stdio: 'pipe' }),
      `${name} does not parse, so nothing in it runs`,
    )
  }
})

test('every name the page can dispatch is a function it defines', () => {
  const source = SCRIPTS.map((name) => readFileSync(path.join(PAGES, name), 'utf8')).join('\n')
  const listed = source.match(/const ACTIONS = new Set\(\[(.*?)\]\)/s)
  assert.ok(listed, 'the dispatcher’s allowlist is gone')

  const names = [...listed[1].matchAll(/'([^']+)'/g)].map((found) => found[1])
  assert.ok(names.length > 50, `the allowlist holds ${names.length} names, which is too few to be the real one`)

  const defined = new Set([
    ...[...source.matchAll(/^\s*(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(/gm)].map((found) => found[1]),
    ...[...source.matchAll(/^\s*(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?(?:\(|function)/gm)].map(
      (found) => found[1],
    ),
    ...[...source.matchAll(/^\s*window\.([A-Za-z0-9_$]+)\s*=/gm)].map((found) => found[1]),
  ])
  const missing = names.filter((name) => !defined.has(name))
  assert.deepEqual(missing, [], `the pages can dispatch ${missing.join(', ')} and define no such function`)
})
