import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

const proxyTarget = { target: 'http://127.0.0.1:8000', changeOrigin: false }
const apiProxy = {
  '/logistics': proxyTarget,
  '/auth': proxyTarget,
  '/stores': proxyTarget,
  '/analysis-runs': proxyTarget,
  '/workflow-runs': proxyTarget,
  '/workbench': proxyTarget,
  '/proposals': proxyTarget,
  '/approvals': proxyTarget,
  '/knowledge': proxyTarget,
  '/agent-evaluations': proxyTarget,
  '/agent-calls': proxyTarget,
  '/audit-events': proxyTarget,
  '/admin': proxyTarget,
}

export default defineConfig({
  base: '/app/',
  plugins: [vue()],
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
  },
  server: { proxy: apiProxy },
})
