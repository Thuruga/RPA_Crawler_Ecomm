import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

// Caminho-base para produção/preview (default "/")
export default defineConfig(({ mode }) => {
  const frontendDir = fileURLToPath(new URL('.', import.meta.url))
  const env = loadEnv(mode, frontendDir, 'VITE_')
  const rawBase = (env.VITE_BASE_PATH || '').trim()
  let base = '/'
  if (rawBase && rawBase !== '/') {
    const withLeading = rawBase.startsWith('/') ? rawBase : `/${rawBase}`
    base = withLeading.endsWith('/') ? withLeading : `${withLeading}/`
  }

  return {
    base,
    envPrefix: ['VITE_'],
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      strictPort: true,
      proxy: {
        '^/(brands(?:/|$)|banners/|map-rules(?:/|$)|search(?:/|$)|history(?:/|$)|monitor/|monitors(?:/|$)|scrape-category(?:-multi)?(?:/|$)|canonical-categories(?:/|$)|download-report/|ws/|system/)': {
          target: 'http://127.0.0.1:8088',
          changeOrigin: true,
          ws: true,
        }
      }
    },
    preview: {
      host: '127.0.0.1',
      strictPort: true,
    },
  }
})
