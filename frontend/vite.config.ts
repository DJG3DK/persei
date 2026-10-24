import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

// Monaco vendors its own DOMPurify (esm/vs/base/browser/dompurify/) and
// imports it from exactly one place, domSanitize.js. That copy lags the
// advisories, and an npm override cannot reach it (audit U25), so the build
// points that one import at the npm package instead: the same API, the
// patched release. CodeEditor.test.ts holds both ends of this in place.
// From the string, not `new URL`: under the jsdom test environment the
// global URL is jsdom's, which fileURLToPath refuses.
const HERE = import.meta.url.startsWith('file:') ? dirname(fileURLToPath(import.meta.url)) : process.cwd()
export const PURIFY = resolve(HERE, 'node_modules/dompurify/dist/purify.es.mjs')

export function monacoDompurify(): Plugin {
  return {
    name: 'monaco-dompurify',
    enforce: 'pre',
    resolveId(source, importer) {
      if (source === './dompurify/dompurify.js'
          && importer?.replace(/\\/g, '/').split('?')[0].endsWith('/vs/base/browser/domSanitize.js')) {
        return PURIFY
      }
      return null
    },
  }
}

// https://vite.dev/config/
export default defineConfig(() => ({
  plugins: [react(), monacoDompurify()],
  // Root, on Persei's own domain. This was '/v2/' for the build while the
  // dashboard shared agent.example.com with another service at '/' --
  // that subpath existed only to avoid provisioning DNS and a cert for a
  // pilot, and perseiagent.ai removed the reason for it (2026-09-15).
  // api.ts/useTaskStream.ts build request paths off import.meta.env.BASE_URL,
  // which Vite sets from this, so nothing else had to change.
  base: '/',
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8100',
        ws: true, // needed for /api/tasks/:id/stream (WebSocket) in addition to plain HTTP routes
      },
    },
  },
}))
