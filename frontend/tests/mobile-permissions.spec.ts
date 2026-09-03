import { expect, test, type Page } from '@playwright/test'
import { describe, test as vitest } from 'vitest'

import { installApiFixture } from './apiFixture'

const isVitest = typeof process !== 'undefined' && process.env.VITEST === 'true'

async function login(page: Page, username: 'operator' | 'supervisor'): Promise<void> {
  await page.goto('/app/login')
  await page.getByLabel('用户名').fill(username)
  await page.getByLabel('密码').fill('fixture-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '任务工作台' })).toBeVisible()
}

if (isVitest) {
  describe.skip('Playwright browser scenarios', () => {
    vitest('runs via npm run test:e2e', () => undefined)
  })
} else {
  test.use({ viewport: { width: 390, height: 844 } })

  test('mobile keeps operator read-only while allowing supervisor approval', async ({ page }) => {
    const fixture = await installApiFixture(page)
    fixture.seedPendingApproval()

    await login(page, 'operator')
    await expect(page.getByRole('link', { name: '经营分析', exact: true })).toHaveCount(0)
    await page.goto('/app/analysis')
    await expect(page.locator('[data-test="start-analysis"]')).toHaveCount(0)
    await page.goto('/app/analysis/analysis-1')
    await expect(page.locator('[data-test="mobile-candidate-candidate-1"]')).toBeVisible()
    await expect(page.locator('[data-test="select-candidate-1"]')).toHaveCount(0)
    await page.goto('/app/proposals/proposal-1')
    await expect(page.locator('[data-test="submit-manual"]')).toHaveCount(0)
    await expect(page.locator('[data-test="submit-proposal"]')).toHaveCount(0)
    await expect(page.locator('[data-test="approve-action"]')).toHaveCount(0)

    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    await login(page, 'supervisor')
    await page.getByRole('link', { name: '待审批', exact: true }).click()
    await page.locator('[data-test="open-proposal-1"]').click()
    await expect(page.locator('[data-test="approve-action"]')).toBeVisible()
    await page.locator('[data-test="approve-action"]').click()
    await page.getByRole('button', { name: '批准', exact: true }).click()
    await expect(page.locator('[data-test="publish-record"]')).toBeVisible()
    fixture.assertProtocol()
  })
}
