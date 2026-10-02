import path from 'node:path'
import { defineConfig, loadEnv } from 'vite'

/* The pages under public/pages/ are classic scripts, copied from the library.
   They share their functions on window, which is how the click dispatcher
   finds them, so they are served as they are and never bundled. Vite bundles
   src/ only, which is where anything we write goes. */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const upstream = env.VITE_DEV_UPSTREAM || 'http://127.0.0.1:8000'
  // Everything the pages call. The dev server forwards these to the Backend,
  // so the browser sees one origin and the sign-in cookie behaves as it will
  // in production. /auth is not under /api: the sign-in routes sit at the root.
  // /brand is the company's colours and logo, set on the retrieval service.
  const forwarded = ['/api', '/auth', '/health', '/openapi.json', '/brand']

  return {
    resolve: {
      alias: { '@': path.resolve(import.meta.dirname, './src') },
    },
    server: {
      port: 5173,
      // Loopback only: a dev server holds a signed-in session and must not be
      // reachable from the network.
      host: '127.0.0.1',
      proxy: Object.fromEntries(
        forwarded.map((prefix) => [
          prefix,
          {
            target: upstream,
            // The Host header is left alone, so the Backend can tell it is
            // behind the dev server, and cookies stay bound to localhost.
            changeOrigin: false,
            ws: false,
          },
        ]),
      ),
    },
    envPrefix: 'VITE_',
    build: {
      outDir: 'dist',
      emptyOutDir: true,
    },
  }
})
