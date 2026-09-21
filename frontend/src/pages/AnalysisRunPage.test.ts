import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { clearSession, setCurrentUser } from '../session'
import type { AnalysisCandidate, WorkflowRun } from '../types'
import AnalysisRunPage from './AnalysisRunPage.vue'

const processingRun: WorkflowRun = {
  id: 'analysis-1',
  workflow_type: 'analysis',
  store_id: 'store-1',
  start_date: '2026-08-01',
  end_date: '2026-08-31',
  status: 'processing',
  quality_status: 'normal',
  current_step: 'aggregate',
  attempt_count: 1,
  candidates_ready: false,
  error_code: null,
}

const readyRun: WorkflowRun = {
  ...processingRun,
  status: 'awaiting_selection',
  current_step: 'rank',
  candidates_ready: true,
}

const candidate: AnalysisCandidate = {
  id: 'candidate-1',
  product_id: 'product-1',
  rank: 1,
  product_code: 'PRODUCT-1',
  anomaly_types: ['low_conversion'],
  metrics: {
    product_id: 'product-1',
    product_code: 'PRODUCT-1',
    impressions: 1000,
    clicks: 125,
    orders: 10,
    units: 12,
    revenue: '1234.50',
    refunds: 1,
    ctr: '0.1250',
    conversion_rate: '0.0800',
    refund_rate: '0.1000',
    average_order_value: '123.4500',
  },
  business_impact: '321.00',
  evidence: ['<img src=x onerror=alert(1)>', '近 30 天转化率低于店铺基线'],
  impact_explanation: '预计影响收入 321.00',
  reason: '流量进入后转化不足',
  recommended_action: '优化标题与详情表达',
  confidence: '0.8800',
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function setMobile(mobile: boolean): void {
  vi.stubGlobal('matchMedia', (media: string) => ({
    matches: mobile,
    media,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }))
}

async function mountRun() {
  setCurrentUser({ id: 'operator-1', username: 'operator', role: 'operator', department: 'operations' })
  const router = createRouter({
    history: createMemoryHistory('/app/'),
    routes: [
      { path: '/analysis/:runId', name: 'analysis-run', component: AnalysisRunPage },
      { path: '/proposals/:proposalId', name: 'proposal', component: { template: '<div />' } },
    ],
  })
  await router.push('/analysis/analysis-1')
  await router.isReady()
  const wrapper = mount(AnalysisRunPage, { global: { plugins: [ElementPlus, router] } })
  await flushPromises()
  return { router, wrapper }
}

afterEach(() => {
  clearSession()
  vi.unstubAllGlobals()
})

describe('AnalysisRunPage', () => {
  it('shows a processing skeleton while the workflow is running', async () => {
    setMobile(false)
    vi.stubGlobal('fetch', async () => response(processingRun))
    const { wrapper } = await mountRun()

    expect(wrapper.get('[data-test="analysis-processing"]').text()).toContain('处理中')
    wrapper.unmount()
  })

  it('renders ranked candidates and evidence as plain text', async () => {
    setMobile(false)
    vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/workflow-runs/analysis-1') return response(readyRun)
      if (path === '/analysis-runs/analysis-1/candidates') return response([candidate])
      throw new Error(`unexpected request: ${path}`)
    })
    const { wrapper } = await mountRun()

    expect(wrapper.get('[data-test="candidate-candidate-1"]').text()).toContain('PRODUCT-1')
    expect(wrapper.get('[data-test="candidate-candidate-1"]').text()).toContain('0.1250')
    expect(wrapper.get('[data-test="candidate-candidate-1"]').text()).toContain('321.00')
    await wrapper.get('[data-test="open-evidence-candidate-1"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('<img src=x onerror=alert(1)>')
    expect(wrapper.find('img').exists()).toBe(false)
    wrapper.unmount()
  })

  it('confirms selection and reuses one idempotency key until success', async () => {
    setMobile(false)
    const idempotencyKeys: string[] = []
    let selectionAttempts = 0
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm')
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/workflow-runs/analysis-1') return response(readyRun)
      if (path === '/analysis-runs/analysis-1/candidates') return response([candidate])
      if (path === '/analysis-runs/analysis-1/select-product') {
        selectionAttempts += 1
        idempotencyKeys.push(new Headers(init?.headers).get('Idempotency-Key') ?? '')
        if (selectionAttempts === 1) {
          return response({ detail: { code: 'OPTIMIZATION_WRITE_FAILED' } }, 503)
        }
        return response(
          {
            proposal_id: 'proposal-1',
            optimization_workflow_run_id: 'optimization-1',
            status: 'accepted',
          },
          202,
        )
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const { router, wrapper } = await mountRun()

    await wrapper.get('[data-test="select-candidate-1"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="selection-error"]').text()).toContain('选品提交失败')
    await wrapper.get('[data-test="retry-selection"]').trigger('click')
    await flushPromises()

    expect(idempotencyKeys).toHaveLength(2)
    expect(idempotencyKeys[0]).toMatch(/^[0-9a-f-]{36}$/)
    expect(idempotencyKeys[1]).toBe(idempotencyKeys[0])
    expect(ElMessageBox.confirm).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.fullPath).toBe('/proposals/proposal-1')
  })

  it('allows only one selection flow while confirmation is pending', async () => {
    setMobile(false)
    let finishConfirmation!: (value: 'confirm') => void
    const confirmation = new Promise<'confirm'>((resolve) => (finishConfirmation = resolve))
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockReturnValue(confirmation)
    let selectionRequests = 0
    vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/workflow-runs/analysis-1') return response(readyRun)
      if (path === '/analysis-runs/analysis-1/candidates') return response([candidate])
      if (path === '/analysis-runs/analysis-1/select-product') {
        selectionRequests += 1
        return response(
          {
            proposal_id: 'proposal-1',
            optimization_workflow_run_id: 'optimization-1',
            status: 'accepted',
          },
          202,
        )
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const { wrapper } = await mountRun()
    const button = wrapper.get('[data-test="select-candidate-1"]')

    await button.trigger('click')
    await button.trigger('click')
    finishConfirmation('confirm')
    await flushPromises()

    expect(confirm).toHaveBeenCalledOnce()
    expect(selectionRequests).toBe(1)
  })

  it('keeps candidate details readable on mobile without a select action', async () => {
    setMobile(true)
    vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/workflow-runs/analysis-1') return response(readyRun)
      if (path === '/analysis-runs/analysis-1/candidates') return response([candidate])
      throw new Error(`unexpected request: ${path}`)
    })
    const { wrapper } = await mountRun()

    expect(wrapper.get('[data-test="mobile-candidate-candidate-1"]').text()).toContain(
      'PRODUCT-1',
    )
    expect(wrapper.find('[data-test="select-candidate-1"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
