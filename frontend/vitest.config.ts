import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Separate from vite.config.ts on purpose. That file's `base` is a deploy
// concern (it was '/v2/' until 2026-09-15 and is '/' now), and tests must not
// inherit it — api.ts derives request paths from import.meta.env.BASE_URL,
// so tests asserting on URLs would have to encode the deploy prefix. Keeping
// the test config standalone means a test says what the code does, not where
// it happens to be mounted.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    // Fail the run on an unhandled rejection rather than printing it and
    // passing — an async assertion that never settles is a real failure.
    dangerouslyIgnoreUnhandledErrors: false,
    coverage: {
      provider: 'v8',
      reportsDirectory: './coverage',
      // Reported, not enforced: a threshold that fails CI on an unrelated
      // refactor teaches people to delete tests. Look at the number instead.
      reporter: ['text-summary', 'html', 'json-summary'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/test/**',
        'src/main.tsx',
        'src/types.ts',
        'src/vite-env.d.ts',
      ],
    },
  },
})
