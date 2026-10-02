/* Calls to our own Backend, for anything we write. The copied pages have
   their own caller inside app.js and do not use this one.

   Same origin: the Backend serves this page and forwards /api and /auth to
   the retrieval service, so the browser sends its session cookie without
   being told to, and no upstream key is ever in the browser. The key below is
   only the one a person typed into the dashboard on a server that has no
   sign-in, which is where the pages keep it too. */

const KEY = 'vectrixdb.apiKey'
const CSRF = 'vx_csrf'

function cookie(name) {
  const found = document.cookie.split('; ').find((part) => part.startsWith(`${name}=`))
  return found ? decodeURIComponent(found.slice(name.length + 1)) : null
}

function typedKey() {
  try {
    return localStorage.getItem(KEY) || null
  } catch {
    return null
  }
}

/**
 * One call to the Backend. Returns the parsed body, or throws with what the
 * service said, which is the shape every refusal takes.
 *
 * @param {string} path a path beginning with /api or /auth
 * @param {RequestInit} [options]
 * @returns {Promise<any>}
 */
export async function ask(path, options = {}) {
  const headers = new Headers(options.headers || {})
  if (options.body && !headers.has('content-type')) headers.set('content-type', 'application/json')
  const key = typedKey()
  if (key) headers.set('api-key', key)
  // The cookie is not enough on a write: the server asks for the token it set
  // beside it, which is what makes another site's form useless.
  const token = cookie(CSRF)
  if (token && options.method && options.method !== 'GET') headers.set('x-csrf-token', token)

  const reply = await fetch(path, { credentials: 'same-origin', ...options, headers })
  const body = reply.headers.get('content-type')?.includes('json') ? await reply.json() : await reply.text()
  if (!reply.ok) throw new Error(typeof body === 'string' ? body : body.detail || body.message || `${reply.status}`)
  return body
}
