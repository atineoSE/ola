import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
// In dev (`npm run dev`) the SPA is served by Vite on :5173; proxy the
// dashboard server's API (default :8765) so same-origin `/api/*` fetches work
// without CORS. In production the `ola-dashboard` server serves the built SPA
// and the API from one origin, so no proxy is needed.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://localhost:8765",
    },
  },
})
