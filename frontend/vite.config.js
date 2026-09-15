import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时代理 /api 到本地后端，避免 CORS；生产由 FastAPI 托管 dist。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
