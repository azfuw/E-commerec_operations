import path from 'node:path'

import { defineConfig } from '@playwright/test'

const baseURL = process.env.PHASE10_E2E_BASE_URL
const evidenceDir = process.env.PHASE10_EVIDENCE_DIR

if (!baseURL) throw new Error('PHASE10_E2E_BASE_URL_REQUIRED')
if (!evidenceDir) throw new Error('PHASE10_EVIDENCE_DIR_REQUIRED')

export default defineConfig({
  testDir: './tests',
  testMatch: 'phase10-real-services.spec.ts',
  outputDir: path.join(evidenceDir, 'playwright'),
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  timeout: 300_000,
  expect: { timeout: 120_000 },
  use: {
    baseURL,
    screenshot: 'only-on-failure',
    trace: 'off',
    serviceWorkers: 'block',
  },
  projects: [{ name: 'chromium-real-services', use: { browserName: 'chromium' } }],
})
