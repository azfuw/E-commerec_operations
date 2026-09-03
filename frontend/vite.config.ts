import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

const proxyTarget = { target: 'http://127.0.0.1:8000', changeOrigin: false }
const apiProxy = {
  '/auth': proxyTarget,
  '/stores': proxyTarget,
  '/analysis-runs': proxyTarget,
  '/workflow-runs': proxyTarget,
  '/workbench': proxyTarget,
  '/proposals': proxyTarget,
  '/approvals': proxyTarget,
}

export default defineConfig({
  base: '/app/',
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
  },
  server: { proxy: apiProxy },
})
