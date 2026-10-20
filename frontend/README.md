# Persei dashboard

The React/Vite frontend for [Persei](../README.md). It is not a standalone
app: `agent/server.py` serves the built bundle from `dist/` itself, so there is
no separate frontend process in production.

## Working on it

```bash
npm ci
npm run dev        # dev server against a backend on :8100
npm run build      # production bundle -> dist/
npx tsc --noEmit -p tsconfig.app.json
npm run lint
```

A frontend change reaches a deployment by rebuilding `dist/` and restarting the
backend — see [INSTALL.md](../INSTALL.md).

## Layout

- `src/components/` — views and panels; each has its own CSS file beside it.
- `src/api.ts` — every call to the backend, and the types they return.
- `src/useTaskStream.ts` / `src/usePlanningStream.ts` — the two WebSocket
  streams. Both hydrate from a snapshot before connecting and re-hydrate on
  reconnect; a socket that reconnects without re-reading the snapshot silently
  loses whatever arrived while it was down.
- `src/theme.css` — colour tokens. Contrast ratios are documented inline and
  are meant to stay at or above 4.5:1.
