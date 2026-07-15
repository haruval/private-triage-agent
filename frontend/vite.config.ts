// Dev-server config for the review UI. Two properties here are load-bearing
// for the threat model (see src/api/server.py):
//
// 1. The server binds 127.0.0.1 with strictPort and Vite's default
//    allowedHosts — never run with --host, which would republish the proxied
//    API to the LAN.
// 2. The per-run session token from frontend/.dev-token is injected into
//    proxied /api requests *here*, server-side. Browser JavaScript never sees
//    it, so a foreign page (CSRF, DNS rebinding) can't steal what the page
//    never had.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const tokenPath = join(dirname(fileURLToPath(import.meta.url)), '.dev-token')

function readToken(): string {
  try {
    return readFileSync(tokenPath, 'utf-8').trim()
  } catch {
    throw new Error(
      'frontend/.dev-token not found — start the API first (`make api`), then run `make web`.',
    )
  }
}

export default defineConfig(({ command }) => {
  if (command === 'serve') {
    readToken() // fail fast with the message above if the API was never started
  }
  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8765',
          changeOrigin: true,
          configure(proxy) {
            proxy.on('proxyReq', (proxyReq) => {
              // Re-read per request so restarting the API (which mints a
              // new token) doesn't require restarting Vite.
              try {
                proxyReq.setHeader('X-Triage-Token', readToken())
              } catch {
                // Token file gone (API stopped) — the request will just 403.
              }
            })
          },
        },
      },
    },
  }
})
