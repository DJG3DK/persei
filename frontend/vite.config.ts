import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(() => ({
  plugins: [react()],
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
