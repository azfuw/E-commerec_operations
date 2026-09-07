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

async function logout(page: Page): Promise<void> {
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '登录智营台' })).toBeVisible()
}

async function createAnalysisAndSelect(page: Page): Promise<void> {
  await page.getByRole('link', { name: '经营分析', exact: true }).click()
  await expect(page.getByRole('heading', { name: '经营分析' })).toBeVisible()
  await page.locator('[data-test="analysis-store"]').click({ force: true })
  await page.getByRole('option', { name: '旗舰店', exact: true }).click()
  const dateInputs = page.getByLabel('分析日期')
  await dateInputs.first().fill('2026-08-01')
  await dateInputs.nth(1).fill('2026-08-31')
  await dateInputs.nth(1).press('Tab')
  await page.locator('[data-test="start-analysis"]').click()
  await expect(page.locator('[data-test="candidate-candidate-1"]')).toBeVisible({ timeout: 8_000 })
  await page.locator('[data-test="select-candidate-1"]').click()
  await page.getByRole('button', { name: '确认', exact: true }).click()
  await expect(page.getByRole('heading', { name: '优化方案' })).toBeVisible()
}

async function reviseAndSubmit(page: Page, title: string): Promise<void> {
  const titleInput = page.locator('[data-test="manual-title"] input')
  await expect(titleInput).toBeVisible()
  await titleInput.fill(title)
  await page.locator('[data-test="submit-manual"]').click()
  await expect(page.locator('[data-test="manual-processing"]')).toBeVisible()
  await expect(page.locator('[data-test="submit-proposal"]')).toBeVisible({ timeout: 8_000 })
  await page.locator('[data-test="submit-proposal"]').click()
  await expect(page.locator('[data-test="pending-reason"]')).toBeVisible()
}

async function openApproval(page: Page): Promise<void> {
  await page.getByRole('link', { name: '待审批', exact: true }).click()
  await expect(page.getByRole('heading', { name: '待审批方案' })).toBeVisible()
  await page.locator('[data-test="open-proposal-1"]').click()
  await expect(page.locator('[data-test="approve-action"]')).toBeVisible()
}

async function approve(page: Page): Promise<void> {
  await page.locator('[data-test="approve-action"]').click()
  await page.getByRole('button', { name: '批准', exact: true }).click()
  await expect(page.locator('[data-test="publish-record"]')).toBeVisible()
}

if (isVitest) {
  describe.skip('Playwright browser scenarios', () => {
    vitest('runs via npm run test:e2e', () => undefined)
  })
} else {
  test('operator completes the desktop analysis-to-local-publish flow', async ({ page }) => {
    const fixture = await installApiFixture(page)

    await login(page, 'operator')
    await createAnalysisAndSelect(page)
    await reviseAndSubmit(page, '首次人工修订标题')
    await logout(page)
    await login(page, 'supervisor')
    await openApproval(page)
    await approve(page)

    await expect(page.locator('[data-test="publish-record"]')).toContainText('7 → 8')
    await expect(page.locator('[data-test="publish-record"]')).toContainText(
      '价格、SKU、库存与真实平台均未变化',
    )
    await expect(page.locator('[data-test="platform-delivery"]')).toContainText('平台投递成功')
    await expect(page.locator('[data-test="platform-delivery"]')).toContainText('operation-1')
    fixture.assertProtocol()
  })

  test('request changes keeps the first manual revision immutable in the fixture before approval', async ({ page }) => {
    const fixture = await installApiFixture(page)

    await login(page, 'operator')
    await createAnalysisAndSelect(page)
    await reviseAndSubmit(page, '第一次人工修订')
    const firstRevision = fixture.firstManualRevision()
    expect(firstRevision).toEqual({ id: 'revision-2', title: '第一次人工修订' })

    await logout(page)
    await login(page, 'supervisor')
    await openApproval(page)
    await page.locator('[data-test="request-changes-action"]').click()
    await page.locator('[data-test="approval-comment"]').fill('请补充更清晰的标题')
    await page.locator('[data-test="confirm-comment"]').click()
    await expect(page.locator('[data-test="submit-manual"]')).toBeVisible()
    await reviseAndSubmit(page, '第二次人工修订')
    await approve(page)

    expect(fixture.firstManualRevision()).toEqual(firstRevision)
    await expect(page.locator('[data-test="publish-record"]')).toContainText('第二次人工修订')
    fixture.assertProtocol()
  })
}
