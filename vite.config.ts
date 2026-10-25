import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端地址默认 8000；本地已另起后端（如 8024）时可用 OFFICE_BACKEND_URL 覆盖
const backendTarget = process.env.OFFICE_BACKEND_URL ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    // Generated deliverables and bundled backend tools are not frontend sources.
    watch: { ignored: ['**/backend/**'] },
    proxy: {
      '/api': backendTarget,
      '/ws': { target: backendTarget.replace(/^http/, 'ws'), ws: true },
      '/actions': 'http://127.0.0.1:8765',
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})
