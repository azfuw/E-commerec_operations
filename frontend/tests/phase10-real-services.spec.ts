import { createHash } from 'node:crypto'
import { writeFile } from 'node:fs/promises'
import path from 'node:path'

import { expect, test, type Frame, type Page } from '@playwright/test'

import type {
  AgentCallList,
  AuditEventList,
  ManualRevisionAccepted,
  ProductSelection,
  ProposalDetail,
  PublishRecord,
  WorkflowRun,
} from '../src/types'

const PASSWORD = 'DemoPass!2026'
const FAILURE_PREFIX = '阶段十连续失败验证'
const ASYNC_TIMEOUT = 180_000
const SAFE_DIAGNOSTIC_STATUSES = new Set([
  'accepted',
  'processing',
  'awaiting_selection',
  'completed',
  'draft_ready',
  'pending_manual',
  'pending_approval',
  'rejected',
  'failed',
])
const SAFE_DIAGNOSTIC_QUALITIES = new Set(['normal', 'partial', 'degraded'])
const SAFE_DIAGNOSTIC_ERRORS = new Set([
  'COMPLIANCE_AGENT_DEGRADED',
  'DEEPSEEK_FORBIDDEN',
  'DEEPSEEK_HTTP_ERROR',
  'DEEPSEEK_KEY_MISSING',
  'DEEPSEEK_RATE_LIMIT',
  'DEEPSEEK_SCHEMA_INVALID',
  'DEEPSEEK_SERVER_ERROR',
  'DEEPSEEK_TIMEOUT',
  'DEEPSEEK_TRANSPORT',
  'DEEPSEEK_UNAUTHORIZED',
  'KNOWLEDGE_DEPENDENCY_ERROR',
  'KNOWLEDGE_DEPENDENCY_TIMEOUT',
  'KNOWLEDGE_LOW_CONFIDENCE',
  'KNOWLEDGE_MODEL_UNAVAILABLE',
  'KNOWLEDGE_ZERO_HIT',
  'LEASE_ATTEMPTS_EXHAUSTED',
  'LEASE_LOST',
  'OPTIMIZATION_AUTHORIZATION_CHANGED',
  'OPTIMIZATION_CHECKPOINT_ERROR',
  'OPTIMIZATION_CONTEXT_INCONSISTENT',
  'OPTIMIZATION_CONTEXT_NOT_FOUND',
  'OPTIMIZATION_DATABASE_ERROR',
  'OPTIMIZATION_FACT_ERROR',
  'OPTIMIZATION_ITERATION_LIMIT',
  'OPTIMIZATION_REPLAY_CONFLICT',
  'PRODUCT_VERSION_CONFLICT',
])
const UNSAFE_RENDERED_TEXT =
  /Authorization\b|Bearer|api_key|raw_response|\bprompt\b|traceback|postgres(?:ql)?:\/\//i
const BUSINESS_PREFIXES = [
  '/auth',
  '/analysis-runs',
  '/workflow-runs',
  '/proposals',
  '/approvals',
  '/knowledge',
  '/agent-calls',
  '/audit-events',
] as const
const SAFE_AGENT_CALL_KEYS = [
  'attempt',
  'call_type',
  'completion_tokens',
  'created_at',
  'duration_ms',
  'error_code',
  'estimated_cost',
  'id',
  'iteration',
  'model',
  'node_name',
  'prompt_tokens',
  'prompt_version',
  'status',
  'store_id',
  'total_tokens',
  'workflow_run_id',
  'workflow_type',
]
const SAFE_COMPLIANCE_REVIEW_KEYS = [
  'citations',
  'deterministic_checks',
  'error_code',
  'id',
  'iteration',
  'passed',
  'quality_status',
  'required_changes',
  'risk_level',
  'semantic_review',
]
const PUBLISH_SNAPSHOT_KEYS = [
  'attributes',
  'current_version',
  'description',
  'search_keywords',
  'selling_points',
  'title',
]

type ProtocolObservation = {
  prefix: (typeof BUSINESS_PREFIXES)[number]
  host: string
  status: number | null
  fromServiceWorker: boolean
}

type SuccessEvidence = {
  proposal_id: string
  product_id: string
  store_id: string
  publish_record_id: string
  delivery_resource_id: string
  external_operation_id: string
  proposal_diff: string
  publish_before: string
  publish_after: string
  base_version: number
  published_version: number
}

type RejectionEvidence = { proposal_id: string; product_id: string }

const protocol: ProtocolObservation[] = []
let successEvidence: SuccessEvidence | null = null
let rejectionEvidence: RejectionEvidence | null = null

function businessPrefix(url: string): (typeof BUSINESS_PREFIXES)[number] | null {
  const pathname = new URL(url).pathname
  return BUSINESS_PREFIXES.find((prefix) => pathname.startsWith(prefix)) ?? null
}

function observeProtocol(page: Page): void {
  page.on('request', (request) => {
    const prefix = businessPrefix(request.url())
    if (!prefix) return
    protocol.push({
      prefix,
      host: new URL(request.url()).host,
      status: null,
      fromServiceWorker: request.serviceWorker() !== null,
    })
  })
  page.on('response', (response) => {
    const prefix = businessPrefix(response.url())
    if (!prefix) return
    protocol.push({
      prefix,
      host: new URL(response.url()).host,
      status: response.status(),
      fromServiceWorker: response.fromServiceWorker(),
    })
  })
}

async function actAndRead<T>(
  page: Page,
  method: string,
  pathname: string,
  action: () => Promise<void>,
  { queryMatches = () => true, afterNavigation = false }: {
    queryMatches?: (url: URL) => boolean
    afterNavigation?: boolean
  } = {},
): Promise<T> {
  let documentReady = !afterNavigation
  const onNavigation = (frame: Frame) => {
    if (frame === page.mainFrame()) documentReady = true
  }
  if (afterNavigation) page.on('framenavigated', onNavigation)
  // Bind a new request: a pending response from before a reload belongs to the old document.
  const bodyPromise = page.waitForRequest(
    (request) => {
      const url = new URL(request.url())
      return documentReady && request.method() === method && url.pathname === pathname && queryMatches(url)
    },
    { timeout: ASYNC_TIMEOUT },
  ).then(async (request) => {
    const response = await request.response()
    expect(response, `${method} ${pathname} did not return a response`).not.toBeNull()
    expect(response!.ok(), `${method} ${pathname} returned ${response!.status()}`).toBe(true)
    // Consume immediately, while the action may still be waiting for navigation.
    return response!.json() as Promise<T>
  })
  try {
    const [body] = await Promise.all([bodyPromise, action()])
    return body
  } finally {
    if (afterNavigation) page.off('framenavigated', onNavigation)
  }
}

async function assertSafePage(page: Page): Promise<void> {
  await expect(page.locator('body')).not.toContainText(UNSAFE_RENDERED_TEXT)
}

async function login(page: Page, username: 'operator' | 'supervisor' | 'admin'): Promise<void> {
  await page.goto('/app/login')
  await page.getByLabel('用户名').fill(username)
  await page.getByLabel('密码').fill(PASSWORD)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '任务工作台' })).toBeVisible()
  await assertSafePage(page)
}

async function logout(page: Page): Promise<void> {
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '登录智营台' })).toBeVisible()
}

async function uploadKnowledge(page: Page): Promise<void> {
  const name = '阶段十合规规则'
  await page.getByRole('link', { name: '知识库', exact: true }).click()
  await expect(page.getByRole('heading', { name: '知识库', exact: true })).toBeVisible()
  const form = page.locator('form').filter({
    has: page.getByRole('button', { name: '上传文档', exact: true }),
  })
  await form.getByLabel('文档名称').fill(name)
  await form.getByLabel('类别', { exact: true }).fill('通用规则')
  await form
    .getByLabel('文件', { exact: true })
    .setInputFiles(path.resolve(process.cwd(), '..', 'tests', 'fixtures', 'phase10-knowledge.md'))
  await actAndRead(page, 'POST', '/knowledge/documents', async () => {
    await form.getByRole('button', { name: '上传文档', exact: true }).click()
  })
  await expect(page.getByText('已接受，等待索引处理', { exact: true })).toBeVisible()

  await expect
    .poll(
      async () => {
        await page.reload({ waitUntil: 'domcontentloaded' })
        return page.getByRole('row').filter({ hasText: name }).textContent().catch(() => '')
      },
      { timeout: ASYNC_TIMEOUT, intervals: [500, 1_000, 2_000] },
    )
    .toContain('active')
  await assertSafePage(page)
}

async function reloadWorkflowUntilCandidates(page: Page, workflowId: string): Promise<void> {
  const pathname = `/workflow-runs/${workflowId}`
  await expect
    .poll(
      async () => {
        const workflow = await actAndRead<WorkflowRun>(page, 'GET', pathname, async () => {
          await page.reload({ waitUntil: 'domcontentloaded' })
        }, { afterNavigation: true })
        if (workflow.status !== 'awaiting_selection' || !workflow.candidates_ready) {
          return false
        }
        return page.locator('[data-test^="select-"]').first().isVisible().catch(() => false)
      },
      { timeout: ASYNC_TIMEOUT, intervals: [500, 1_000, 2_000] },
    )
    .toBe(true)
}

async function reloadProposalUntil(
  page: Page,
  proposalId: string,
  ready: (detail: ProposalDetail) => boolean,
  stopStatuses: ReadonlySet<ProposalDetail['optimization_run']['status']> = new Set(),
): Promise<ProposalDetail> {
  const pathname = `/proposals/${proposalId}`
  const deadline = Date.now() + ASYNC_TIMEOUT
  let diagnostic = 'status=unknown quality=unknown error_code=unknown'
  while (Date.now() < deadline) {
    const detail = await actAndRead<ProposalDetail>(page, 'GET', pathname, async () => {
      await page.reload({ waitUntil: 'domcontentloaded' })
    }, { afterNavigation: true })
    const { status, quality_status: quality, error_code: errorCode } = detail.optimization_run
    diagnostic = [
      `status=${SAFE_DIAGNOSTIC_STATUSES.has(status) ? status : 'unknown'}`,
      `quality=${SAFE_DIAGNOSTIC_QUALITIES.has(quality) ? quality : 'unknown'}`,
      `error_code=${errorCode === null ? 'none' : SAFE_DIAGNOSTIC_ERRORS.has(errorCode) ? errorCode : 'unknown'}`,
    ].join(' ')
    if (ready(detail)) return detail
    if (stopStatuses.has(status)) throw new Error(`PHASE10_PROPOSAL_TERMINAL ${diagnostic}`)
    await page.waitForTimeout(500)
  }
  throw new Error(`PHASE10_PROPOSAL_STATUS_TIMEOUT ${diagnostic}`)
}

async function createDraftProposal(page: Page): Promise<ProposalDetail> {
  await page.getByRole('link', { name: '经营分析', exact: true }).click()
  await expect(page.getByRole('heading', { name: '经营分析', exact: true })).toBeVisible()
  await page.locator('[data-test="analysis-store"]').click({ force: true })
  await page.getByRole('option', { name: '旗舰店', exact: true }).click()
  const dates = page.getByLabel('分析日期')
  await dates.first().fill('2026-07-26')
  await dates.nth(1).fill('2026-08-24')
  await dates.nth(1).press('Tab')
  const analysis = await actAndRead<{ workflow_run_id: string }>(
    page,
    'POST',
    '/analysis-runs',
    async () => page.locator('[data-test="start-analysis"]').click(),
  )
  await expect(page).toHaveURL(new RegExp(`/app/analysis/${analysis.workflow_run_id}$`))
  await reloadWorkflowUntilCandidates(page, analysis.workflow_run_id)

  const selection = await actAndRead<ProductSelection>(
    page,
    'POST',
    `/analysis-runs/${analysis.workflow_run_id}/select-product`,
    async () => {
      await page.locator('[data-test^="select-"]').first().click()
      await page.getByRole('button', { name: '确认', exact: true }).click()
    },
  )
  await expect(page).toHaveURL(new RegExp(`/app/proposals/${selection.proposal_id}$`))
  const detail = await reloadProposalUntil(
    page,
    selection.proposal_id,
    (value) => value.optimization_run.status === 'draft_ready' && value.current_review?.passed === true,
    new Set(['pending_manual', 'failed']),
  )
  await expect(page.locator('[data-test="submit-proposal"]')).toBeVisible()
  await assertSafePage(page)
  return detail
}

async function submitCurrentDraft(page: Page, proposalId: string): Promise<void> {
  await actAndRead(page, 'POST', `/proposals/${proposalId}/submit`, async () => {
    await page.locator('[data-test="submit-proposal"]').click()
  })
  await expect(page.locator('[data-test="pending-reason"]')).toBeVisible()
}

async function openApproval(page: Page, proposalId: string): Promise<void> {
  await page.getByRole('link', { name: '待审批', exact: true }).click()
  await expect(page.getByRole('heading', { name: '待审批方案', exact: true })).toBeVisible()
  await page.locator(`[data-test="open-${proposalId}"]`).click()
  await expect(page.locator('[data-test="approve-action"]')).toBeVisible()
}

async function requestChanges(page: Page, proposalId: string): Promise<void> {
  await actAndRead(page, 'POST', `/approvals/${proposalId}/request-changes`, async () => {
    await page.locator('[data-test="request-changes-action"]').click()
    await page.locator('[data-test="approval-comment"]').fill('请进行阶段十人工复核')
    await page.locator('[data-test="confirm-comment"]').click()
  })
  await reloadProposalUntil(
    page,
    proposalId,
    (detail) => detail.optimization_run.status === 'pending_manual',
  )
}

async function submitManualRevision(
  page: Page,
  proposalId: string,
  title: string,
): Promise<ManualRevisionAccepted> {
  await page.locator('[data-test="manual-title"] input').fill(title)
  return actAndRead(page, 'POST', `/proposals/${proposalId}/manual-revision`, async () => {
    await page.locator('[data-test="submit-manual"]').click()
  })
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    return `{${Object.entries(value)
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(',')}}`
  }
  return JSON.stringify(value)
}

function assertSafeComplianceReview(detail: ProposalDetail): void {
  expect(detail.current_review).not.toBeNull()
  expect(Object.keys(detail.current_review!).sort()).toEqual(SAFE_COMPLIANCE_REVIEW_KEYS)
  expect(JSON.stringify(detail.current_review)).not.toMatch(
    /"(?:authorization|api_key|raw_response|prompt|traceback)"\s*:/i,
  )
}

async function gotoProposal(page: Page, proposalId: string): Promise<ProposalDetail> {
  return actAndRead(page, 'GET', `/proposals/${proposalId}`, async () => {
    await page.goto(`/app/proposals/${proposalId}`)
  }, { afterNavigation: true })
}

test.beforeEach(async ({ page }) => observeProtocol(page))

test.afterEach(async ({ page }) => assertSafePage(page))

test.afterAll(() => {
  for (const prefix of BUSINESS_PREFIXES) {
    expect(protocol.some((item) => item.prefix === prefix), `${prefix} was not exercised`).toBe(true)
  }
  expect(protocol.every((item) => item.host === '127.0.0.1:4174')).toBe(true)
  expect(protocol.every((item) => !item.fromServiceWorker)).toBe(true)
  expect(
    protocol
      .filter((item) => item.status !== null)
      .every((item) => item.status! >= 200 && item.status! < 300),
  ).toBe(true)
})

test.describe.serial('Phase 10 real local services', () => {
  test('recovers one accepted platform mutation through the actual UI', async ({ page }) => {
    await login(page, 'admin')
    await uploadKnowledge(page)
    await logout(page)

    await login(page, 'operator')
    const draft = await createDraftProposal(page)
    const proposalId = draft.proposal.id
    await submitCurrentDraft(page, proposalId)
    await logout(page)

    await login(page, 'supervisor')
    await openApproval(page, proposalId)
    await requestChanges(page, proposalId)
    await logout(page)

    await login(page, 'operator')
    await gotoProposal(page, proposalId)
    const manualTitle = '阶段十平台恢复人工修订标题'
    const accepted = await submitManualRevision(page, proposalId, manualTitle)
    const reviewed = await reloadProposalUntil(
      page,
      proposalId,
      (detail) =>
        detail.current_revision?.id === accepted.revision_id &&
        detail.optimization_run.status === 'draft_ready' &&
        detail.current_review?.passed === true,
    )
    await expect(page.getByRole('heading', { name: '合规结果' }).locator('..')).toContainText('已通过')
    const proposalDiff = await page.locator('[data-test="proposal-diff"]').innerText()
    await submitCurrentDraft(page, proposalId)
    await logout(page)

    await login(page, 'supervisor')
    await openApproval(page, proposalId)
    const published = await actAndRead<PublishRecord>(
      page,
      'POST',
      `/approvals/${proposalId}/approve`,
      async () => {
        await page.locator('[data-test="approve-action"]').click()
        await page.getByRole('button', { name: '批准', exact: true }).click()
      },
    )
    const delivered = await reloadProposalUntil(
      page,
      proposalId,
      (detail) => detail.publish_record?.platform_delivery?.status === 'succeeded',
    )
    const record = delivered.publish_record!
    const delivery = record.platform_delivery!
    expect(record.id).toBe(published.id)
    expect(record.published_product_version).toBe(record.base_product_version + 1)
    expect(delivery.attempt_count).toBe(2)
    expect(delivery.external_operation_id).toMatch(/^[A-Za-z0-9_-]+$/)
    expect(Object.keys(record.before_snapshot).sort()).toEqual(PUBLISH_SNAPSHOT_KEYS)
    expect(Object.keys(record.after_snapshot).sort()).toEqual(PUBLISH_SNAPSHOT_KEYS)
    await expect(page.locator('[data-test="publish-record"]')).toHaveCount(1)
    await expect(page.locator('[data-test="publish-record"]')).toContainText(
      `商品版本 ${record.base_product_version} → ${record.published_product_version}`,
    )
    await expect(page.locator('[data-test="publish-before"]')).toContainText(
      String(reviewed.current_revision!.proposal_output.changes.find((item) => item.field === 'title')!.current_value),
    )
    await expect(page.locator('[data-test="publish-after"]')).toContainText(manualTitle)
    await expect(page.locator('[data-test="proposal-diff"]')).toHaveText(proposalDiff, { useInnerText: true })
    await expect(page.locator('[data-test="publish-record"]')).toContainText(
      '价格、SKU、库存与真实平台均未变化',
    )
    await expect(page.locator('[data-test="platform-delivery"]')).toContainText('平台投递成功')
    await expect(page.locator('[data-test="platform-delivery"]')).toContainText('尝试次数 2')
    await expect(page.locator('[data-test="platform-delivery"]')).toContainText(
      delivery.external_operation_id!,
    )
    await expect(
      page.getByText(`外部操作编号 ${delivery.external_operation_id}`, { exact: true }),
    ).toHaveCount(1)

    await page.getByRole('link', { name: 'Agent 评测', exact: true }).click()
    const calls = await actAndRead<AgentCallList>(page, 'GET', '/agent-calls', async () => {
      await page.getByRole('tab', { name: 'Agent 调用', exact: true }).click()
    })
    expect(calls.items.length).toBeGreaterThan(0)
    for (const call of calls.items) expect(Object.keys(call).sort()).toEqual(SAFE_AGENT_CALL_KEYS)
    await expect(page.locator('.el-table')).toContainText(calls.items[0]!.node_name)
    await expect(page.locator('.el-table')).toContainText(calls.items[0]!.prompt_version)

    const audits = await actAndRead<AuditEventList>(page, 'GET', '/audit-events', async () => {
      await page.getByRole('link', { name: '审计日志', exact: true }).click()
    })
    const enqueued = audits.items.find(
      (item) =>
        item.event_type === 'platform_delivery_enqueued' &&
        item.proposal_id === proposalId &&
        item.publish_record_id === record.id,
    )
    const completed = audits.items.find(
      (item) =>
        item.event_type === 'platform_delivery_completed' &&
        item.publish_record_id === record.id &&
        item.resource_type === 'platform_delivery' &&
        item.resource_id,
    )
    expect(enqueued).toBeTruthy()
    expect(completed).toBeTruthy()
    const completedRow = page.getByRole('row').filter({ hasText: 'platform_delivery_completed' })
    await expect(completedRow).toBeVisible()
    await completedRow.getByRole('button', { name: '查看详情', exact: true }).click()
    await expect(page.getByRole('heading', { name: '操作详情' })).toBeVisible()
    await expect(page.locator('.el-drawer')).toContainText(record.id)
    await expect(page.locator('.el-drawer')).toContainText(completed!.resource_id!)

    await gotoProposal(page, proposalId)
    await expect(page.locator('[data-test="platform-delivery"]')).toContainText('平台投递成功')
    await assertSafePage(page)
    await page.screenshot({
      path: path.join(process.env.PHASE10_EVIDENCE_DIR!, 'e2e-platform-recovered.png'),
      fullPage: true,
    })

    const publishBefore = await page.locator('[data-test="publish-before"]').innerText()
    const publishAfter = await page.locator('[data-test="publish-after"]').innerText()
    successEvidence = {
      proposal_id: proposalId,
      product_id: delivered.proposal.product_id,
      store_id: delivered.proposal.store_id,
      publish_record_id: record.id,
      delivery_resource_id: completed!.resource_id!,
      external_operation_id: delivery.external_operation_id!,
      proposal_diff: proposalDiff,
      publish_before: publishBefore,
      publish_after: publishAfter,
      base_version: record.base_product_version,
      published_version: record.published_product_version,
    }
  })

  test('rejects an independent proposal without platform delivery', async ({ page }) => {
    expect(successEvidence).not.toBeNull()
    await login(page, 'operator')
    const draft = await createDraftProposal(page)
    const proposalId = draft.proposal.id
    await submitCurrentDraft(page, proposalId)
    await logout(page)

    await login(page, 'supervisor')
    await openApproval(page, proposalId)
    await actAndRead(page, 'POST', `/approvals/${proposalId}/reject`, async () => {
      await page.locator('[data-test="reject-action"]').click()
      await page.locator('[data-test="approval-comment"]').fill('阶段十合成驳回意见')
      await page.locator('[data-test="confirm-comment"]').click()
    })
    const rejected = await reloadProposalUntil(
      page,
      proposalId,
      (detail) => detail.optimization_run.status === 'rejected',
    )
    expect(rejected.latest_action?.action).toBe('reject')
    expect(rejected.publish_record).toBeNull()
    expect(rejected.proposal.store_id).toBe(successEvidence!.store_id)
    await expect(page.locator('[data-test="publish-record"]')).toHaveCount(0)
    await expect(page.locator('[data-test="platform-delivery"]')).toHaveCount(0)

    await page.getByRole('link', { name: '审计日志', exact: true }).click()
    await expect(page.getByRole('heading', { name: '审计日志', exact: true })).toBeVisible()
    await page.getByLabel('方案 ID', { exact: true }).fill(proposalId)
    await page.getByRole('combobox', { name: '审批动作', exact: true }).selectOption('reject')
    const rejectionAudits = await actAndRead<AuditEventList>(
      page,
      'GET',
      '/audit-events',
      async () => { await page.getByRole('button', { name: '筛选', exact: true }).click() },
      { queryMatches: (url) =>
        url.searchParams.get('proposal_id') === proposalId &&
        url.searchParams.get('action') === 'reject' },
    )
    expect(rejectionAudits.items).toHaveLength(1)
    expect(rejectionAudits.items[0]).toMatchObject({
      event_type: 'proposal_rejected',
      proposal_id: proposalId,
      approval_action_id: rejected.latest_action!.id,
    })
    await expect(page.locator('.el-table')).toContainText('proposal_rejected')

    await page.getByLabel('方案 ID', { exact: true }).fill('')
    await page.getByRole('combobox', { name: '审批动作', exact: true }).selectOption('')
    await page.getByLabel('审计店铺 ID', { exact: true }).fill(successEvidence!.store_id)
    const storeAudits = await actAndRead<AuditEventList>(
      page,
      'GET',
      '/audit-events',
      async () => { await page.getByRole('button', { name: '筛选', exact: true }).click() },
      { queryMatches: (url) =>
        url.searchParams.get('store_id') === successEvidence!.store_id &&
        !url.searchParams.has('proposal_id') &&
        !url.searchParams.has('action') },
    )
    const completions = storeAudits.items.filter(
      (item) => item.event_type === 'platform_delivery_completed',
    )
    expect(completions).toHaveLength(1)
    expect(completions[0]).toMatchObject({
      proposal_id: null,
      publish_record_id: successEvidence!.publish_record_id,
      resource_type: 'platform_delivery',
      resource_id: successEvidence!.delivery_resource_id,
    })

    const original = await gotoProposal(page, successEvidence!.proposal_id)
    expect(original.publish_record?.id).toBe(successEvidence!.publish_record_id)
    expect(original.publish_record?.base_product_version).toBe(successEvidence!.base_version)
    expect(original.publish_record?.published_product_version).toBe(
      successEvidence!.published_version,
    )
    await expect(page.locator('[data-test="platform-delivery"]')).toContainText(
      successEvidence!.external_operation_id,
    )
    await expect(page.locator('[data-test="proposal-diff"]')).toHaveText(
      successEvidence!.proposal_diff,
      { useInnerText: true },
    )
    await expect(page.locator('[data-test="publish-before"]')).toHaveText(
      successEvidence!.publish_before,
      { useInnerText: true },
    )
    await expect(page.locator('[data-test="publish-after"]')).toHaveText(
      successEvidence!.publish_after,
      { useInnerText: true },
    )
    rejectionEvidence = { proposal_id: proposalId, product_id: rejected.proposal.product_id }
  })

  test('keeps two failed manual revisions immutable and in human control', async ({ page }) => {
    expect(successEvidence).not.toBeNull()
    expect(rejectionEvidence).not.toBeNull()
    await login(page, 'operator')
    const draft = await createDraftProposal(page)
    const proposalId = draft.proposal.id
    await submitCurrentDraft(page, proposalId)
    await logout(page)

    await login(page, 'supervisor')
    await openApproval(page, proposalId)
    await requestChanges(page, proposalId)
    await logout(page)

    await login(page, 'operator')
    await gotoProposal(page, proposalId)
    const firstAccepted = await submitManualRevision(page, proposalId, `${FAILURE_PREFIX}一`)
    const firstFailed = await reloadProposalUntil(
      page,
      proposalId,
      (detail) =>
        detail.current_revision?.id === firstAccepted.revision_id &&
        detail.optimization_run.status === 'pending_manual' &&
        detail.current_review?.passed === false,
    )
    const firstOutput = firstFailed.current_revision!.proposal_output
    const firstHash = createHash('sha256').update(canonicalJson(firstOutput), 'utf8').digest('hex')
    assertSafeComplianceReview(firstFailed)
    await expect(page.getByRole('heading', { name: '合规结果' }).locator('..')).toContainText('未通过')
    await expect(page.locator('[data-test="required-change-title"]')).toBeVisible()
    await expect(page.locator('[data-test="manual-title"] input')).toHaveValue(
      `${FAILURE_PREFIX}一`,
    )

    const secondAccepted = await submitManualRevision(page, proposalId, `${FAILURE_PREFIX}二`)
    const secondFailed = await reloadProposalUntil(
      page,
      proposalId,
      (detail) =>
        detail.current_revision?.id === secondAccepted.revision_id &&
        detail.optimization_run.status === 'pending_manual' &&
        detail.current_review?.passed === false,
    )
    expect(secondAccepted.revision_id).not.toBe(firstAccepted.revision_id)
    expect(secondFailed.latest_action?.action).toBe('request_changes')
    expect(secondFailed.publish_record).toBeNull()
    assertSafeComplianceReview(secondFailed)
    await expect(page.getByRole('heading', { name: '合规结果' }).locator('..')).toContainText('未通过')
    await expect(page.locator('[data-test="required-change-title"]')).toBeVisible()
    await expect(page.locator('[data-test="manual-title"] input')).toHaveValue(
      `${FAILURE_PREFIX}二`,
    )
    await expect(page.locator('[data-test="submit-proposal"]')).toHaveCount(0)
    await expect(page.locator('[data-test="approve-action"]')).toHaveCount(0)
    await expect(page.locator('[data-test="publish-record"]')).toHaveCount(0)
    await expect(page.locator('[data-test="platform-delivery"]')).toHaveCount(0)

    await writeFile(
      path.join(process.env.PHASE10_EVIDENCE_DIR!, 'browser-proof.json'),
      `${JSON.stringify(
        {
          success: {
            proposal_id: successEvidence!.proposal_id,
            product_id: successEvidence!.product_id,
            publish_record_id: successEvidence!.publish_record_id,
          },
          rejection: rejectionEvidence,
          manual_failures: {
            proposal_id: proposalId,
            product_id: secondFailed.proposal.product_id,
            revision_ids: [firstAccepted.revision_id, secondAccepted.revision_id],
            first_revision_sha256: firstHash,
          },
        },
        null,
        2,
      )}\n`,
      'utf8',
    )
  })
})
