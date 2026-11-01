import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The UI only ever calls relative /api/... URLs. In development, Vite's proxy forwards
// them to the API on localhost; in production, Caddy does the same. The browser never
// talks to the backend's own port.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8787', changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
