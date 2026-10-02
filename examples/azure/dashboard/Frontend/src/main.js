/* Our own module entry, the only file Vite bundles. Anything we write goes
   here or under src/; the pages under public/pages/ are the copies and stay
   classic scripts.

   It has one job today. The pages are loaded as separate script tags, and if
   one of them fails to load the page comes up blank with nothing but a
   console line, which reads like a broken server rather than a broken build.
   This says so on the page instead. A module is deferred, so by the time it
   runs the pages have had their chance. */

const LOADED = ['go', 'loadOverview', 'runSearch']

function missing() {
  return LOADED.filter((name) => typeof window[name] !== 'function')
}

function sayItPlainly(names) {
  const note = document.createElement('div')
  note.setAttribute('role', 'alert')
  note.style.cssText = 'position:fixed;inset:auto 16px 16px 16px;z-index:99;padding:12px 14px;border-radius:8px;background:#7f1d1d;color:#fff;font:14px/1.4 system-ui'
  note.textContent = `The dashboard's own scripts did not load (${names.join(', ')} missing). The pages live in public/pages/; check the script tags in index.html and the browser's network tab.`
  document.body.appendChild(note)
}

const absent = missing()
if (absent.length) sayItPlainly(absent)
