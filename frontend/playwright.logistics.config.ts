import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const python = process.env.LOGISTICS_PYTHON || 'python'

export default defineConfig({
  testDir: './tests',
  testMatch: 'logistics-real.spec.ts',
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:8011/app/', viewport: { width: 1440, height: 960 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: `"${python}" -m scripts.run_logistics_demo --port 8011 --data-dir data/logistics-demo/e2e`,
    cwd: root,
    url: 'http://127.0.0.1:8011/health/live',
    reuseExistingServer: false,
    timeout: 60_000,
  },
})
