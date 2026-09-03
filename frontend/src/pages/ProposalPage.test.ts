import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { clearSession, setCurrentUser } from '../session'
import type { ProposalDetail, UserRole, WorkflowStatus } from '../types'
import ProposalPage from './ProposalPage.vue'

function proposalDetail(status: WorkflowStatus = 'pending_manual'): ProposalDetail {
  return {
    proposal: {
      id: 'proposal-1',
      analysis_run_id: 'analysis-1',
      analysis_candidate_id: 'candidate-1',
      optimization_run_id: 'optimization-1',
      store_id: 'store-1',
      product_id: 'product-1',
      base_product_version: 7,
      current_revision_id: 'revision-2',
      created_at: '2026-09-01T08:00:00Z',
      updated_at: '2026-09-01T09:00:00Z',
    },
    optimization_run: {
      id: 'optimization-1',
      workflow_type: 'optimization',
      status,
      quality_status: 'normal',
      error_code: null,
    },
    current_revision: {
      id: 'revision-2',
      iteration: 1,
      revision_number: 2,
      origin: 'agent',
      created_by: 'operator-1',
      parent_revision_id: 'revision-1',
      base_product_version: 7,
      proposal_output: {
        title: '父版本标题',
        selling_points: ['父版本卖点'],
        description: [
          {
            heading: '商品详情',
            body: '父版本详情',
            evidence: [{ kind: 'citation', value: 'chunk-1' }],
          },
        ],
        keywords: ['父版本关键词'],
        attribute_completions: [
          {
            target_attribute: '材质',
            current_value: '棉',
            suggested_value: '精梳棉',
            reason: '补全材质',
            evidence: [{ kind: 'fact', value: 'product.attributes.材质' }],
          },
        ],
        changes: [
          {
            field: 'title',
            current_value: '原商品标题',
            suggested_value: '父版本标题',
            reason: '优化标题表达',
            evidence: [{ kind: 'fact', value: 'product.title' }],
          },
          {
            field: 'selling_points',
            current_value: ['原卖点'],
            suggested_value: ['父版本卖点'],
            reason: '优化卖点表达',
            evidence: [{ kind: 'fact', value: 'product.selling_points' }],
          },
          {
            field: 'description',
            current_value: '原始详情',
            suggested_value: [
              {
                heading: '商品详情',
                body: '父版本详情',
                evidence: [{ kind: 'citation', value: 'chunk-1' }],
              },
            ],
            reason: '优化详情表达',
            evidence: [{ kind: 'citation', value: 'chunk-1' }],
          },
          {
            field: 'keywords',
            current_value: ['原关键词'],
            suggested_value: ['父版本关键词'],
            reason: '优化搜索词',
            evidence: [{ kind: 'fact', value: 'product.search_keywords' }],
          },
        ],
        citations: [{ chunk_id: 'chunk-1' }],
        price_suggestions: [
          {
            target_sku_id: 'sku-1',
            current_price: '100.00',
            suggested_price: '90.00',
            reason: '价格建议',
            evidence: [{ kind: 'citation', value: 'chunk-1' }],
          },
        ],
        sku_suggestions: [
          {
            target_sku_id: 'sku-1',
            current_code: 'SKU-RED',
            current_spec: { 颜色: '红' },
            suggested_code: 'SKU-RED-NEW',
            suggested_spec: { 颜色: '红色' },
            reason: 'SKU 建议',
            evidence: [{ kind: 'citation', value: 'chunk-1' }],
          },
        ],
      },
      citations: [
        {
          document_id: 'document-1',
          version_id: 'version-1',
          chunk_id: 'chunk-1',
          document_name: '通用规则',
          version_number: 1,
          category: '通用规则',
          canonical_text: '<script>alert(1)</script> 仅展示可信事实',
          active: true,
          applicable: true,
        },
      ],
    },
    current_review: {
      id: 'review-2',
      iteration: 1,
      deterministic_checks: { passed: false },
      semantic_review: { passed: false },
      passed: false,
      risk_level: 'medium',
      quality_status: 'normal',
      required_changes: [
        {
          source_track: 'semantic',
          source_violation_code: 'MISLEADING',
          field: 'title',
          instruction: '删除无法证明的绝对化表述',
          citation_chunk_ids: ['chunk-1'],
        },
      ],
      citations: [{ chunk_id: 'chunk-1' }],
      error_code: null,
    },
    active_manual_review: null,
    submitted_revision: null,
    latest_action: null,
    publish_record: null,
  }
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

async function mountProposal(role: UserRole = 'operator') {
  setCurrentUser({ id: `${role}-1`, username: role, role })
  const router = createRouter({
    history: createMemoryHistory('/app/'),
    routes: [
      { path: '/proposals/:proposalId', name: 'proposal', component: ProposalPage },
      { path: '/proposals', name: 'proposals', component: { template: '<div />' } },
    ],
  })
  await router.push('/proposals/proposal-1')
  await router.isReady()
  return {
    router,
    wrapper: mount(ProposalPage, { global: { plugins: [ElementPlus, router] } }),
  }
}

afterEach(() => {
  clearSession()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('ProposalPage', () => {
  it('shows a skeleton and a five-stage partial state without inventing history', async () => {
    setMobile(false)
    let finish!: (value: Response) => void
    vi.stubGlobal('fetch', () => new Promise<Response>((resolve) => (finish = resolve)))
    const { wrapper } = await mountProposal()
    expect(wrapper.get('[data-test="proposal-loading"]').exists()).toBe(true)

    const partial = proposalDetail('processing')
    partial.current_revision = null
    partial.current_review = null
    finish(response(partial))
    await flushPromises()

    expect(wrapper.findAll('[data-test="timeline-stage"]')).toHaveLength(5)
    expect(wrapper.get('[data-test="proposal-partial"]').text()).toContain('当前方案尚未生成')
    wrapper.unmount()
  })

  it('shows trusted diffs, required changes, citations, and readonly price and SKU advice', async () => {
    setMobile(false)
    vi.stubGlobal('fetch', async () => response(proposalDetail()))
    const { wrapper } = await mountProposal()
    await flushPromises()

    expect(wrapper.get('[data-test="proposal-diff-current"]').text()).toContain('原商品标题')
    expect(wrapper.get('[data-test="proposal-diff-suggested"]').text()).toContain('父版本标题')
    expect(wrapper.get('[data-test="required-change-title"]').text()).toContain(
      '删除无法证明的绝对化表述',
    )
    expect(wrapper.get('[data-test="citation-chunk-1"]').text()).toContain(
      '<script>alert(1)</script>',
    )
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.get('[data-test="readonly-price"]').text()).toContain('本阶段不会应用')
    expect(wrapper.get('[data-test="readonly-sku"]').text()).toContain('本阶段不会应用')
    expect(wrapper.find('[data-test="readonly-price"] input').exists()).toBe(false)
    expect(wrapper.find('[data-test="readonly-sku"] input').exists()).toBe(false)
    wrapper.unmount()
  })

  it('disables pending manual writes, retries with one key, and refreshes after review terminal', async () => {
    vi.useFakeTimers()
    setMobile(false)
    let proposalReads = 0
    let manualWrites = 0
    let finishFirstWrite!: (value: Response) => void
    const keys: string[] = []
    const bodies: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/proposals/proposal-1' && !init?.method) {
        proposalReads += 1
        return response(proposalDetail())
      }
      if (path === '/proposals/proposal-1/manual-revision') {
        manualWrites += 1
        keys.push(new Headers(init?.headers).get('Idempotency-Key') ?? '')
        bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>)
        if (manualWrites === 1) {
          return new Promise<Response>((resolve) => (finishFirstWrite = resolve))
        }
        return response(
          {
            revision_id: 'revision-3',
            manual_review_workflow_run_id: 'manual-1',
            status: 'accepted',
          },
          202,
        )
      }
      if (path === '/workflow-runs/manual-1') {
        return response({
          id: 'manual-1',
          workflow_type: 'manual_review',
          store_id: 'store-1',
          start_date: null,
          end_date: null,
          status: 'completed',
          quality_status: 'normal',
          current_step: 'completed',
          attempt_count: 1,
          candidates_ready: false,
          error_code: null,
        })
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const { wrapper } = await mountProposal()
    await flushPromises()
    await wrapper.get('[data-test="manual-title"] input').setValue('人工修订标题')
    await wrapper.get('[data-test="submit-manual"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="submit-manual"]').attributes('disabled')).toBeDefined()

    finishFirstWrite(response({ detail: { code: 'MANUAL_REVIEW_WRITE_FAILED' } }, 503))
    await flushPromises()
    await wrapper.get('[data-test="retry-manual"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2_000)
    await flushPromises()

    expect(keys).toHaveLength(2)
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/)
    expect(keys[1]).toBe(keys[0])
    expect(bodies[0]).not.toHaveProperty('citations')
    expect(bodies[0]).not.toHaveProperty('price_suggestions')
    expect(bodies[0]).not.toHaveProperty('sku_suggestions')
    expect(proposalReads).toBeGreaterThanOrEqual(2)
    wrapper.unmount()
  })

  it('retries proposal submission with the same key and revision', async () => {
    setMobile(false)
    const keys: string[] = []
    const bodies: unknown[] = []
    let writes = 0
    let finishFirstWrite!: (value: Response) => void
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/proposals/proposal-1' && !init?.method) {
        return response(proposalDetail('draft_ready'))
      }
      if (path === '/proposals/proposal-1/submit') {
        writes += 1
        keys.push(new Headers(init?.headers).get('Idempotency-Key') ?? '')
        bodies.push(JSON.parse(String(init?.body)))
        if (writes === 1) {
          return new Promise<Response>((resolve) => (finishFirstWrite = resolve))
        }
        return response({
          id: 'action-1',
          proposal_id: 'proposal-1',
          proposal_revision_id: 'revision-2',
          actor_id: 'operator-1',
          actor_role: 'operator',
          action: 'submit',
          comment: null,
          created_at: '2026-09-03T08:00:00Z',
        }, 201)
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const { wrapper } = await mountProposal()
    await flushPromises()
    await wrapper.get('[data-test="submit-proposal"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="submit-proposal"]').attributes('disabled')).toBeDefined()
    finishFirstWrite(response({ detail: { code: 'APPROVAL_WRITE_FAILED' } }, 503))
    await flushPromises()
    await wrapper.get('[data-test="retry-submit"]').trigger('click')
    await flushPromises()

    expect(keys).toHaveLength(2)
    expect(keys[1]).toBe(keys[0])
    expect(bodies).toEqual([
      { revision_id: 'revision-2' },
      { revision_id: 'revision-2' },
    ])
    wrapper.unmount()
  })

  it('explains pending approval without rendering write actions', async () => {
    setMobile(false)
    vi.stubGlobal('fetch', async () => response(proposalDetail('pending_approval')))
    const { wrapper } = await mountProposal()
    await flushPromises()

    expect(wrapper.get('[data-test="pending-reason"]').text()).toContain('等待主管或管理员审批')
    expect(wrapper.find('[data-test="submit-manual"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="submit-proposal"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps draft details readonly on mobile', async () => {
    setMobile(true)
    vi.stubGlobal('fetch', async () => response(proposalDetail('draft_ready')))
    const { wrapper } = await mountProposal()
    await flushPromises()

    expect(wrapper.get('[data-test="proposal-diff"]').text()).toContain('父版本标题')
    expect(wrapper.find('[data-test="submit-manual"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="submit-proposal"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('resumes a server-reported active manual review and refreshes on terminal state', async () => {
    vi.useFakeTimers()
    setMobile(false)
    let proposalReads = 0
    let workflowReads = 0
    vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/proposals/proposal-1') {
        proposalReads += 1
        const current = proposalDetail()
        if (proposalReads === 1) {
          current.active_manual_review = {
            manual_review_run_id: 'manual-run-restored',
            workflow_run_id: 'manual-restored',
            proposal_revision_id: 'revision-2',
            status: 'processing',
            quality_status: 'normal',
            current_step: 'semantic_review',
            error_code: null,
          }
        }
        return response(current)
      }
      if (path === '/workflow-runs/manual-restored') {
        workflowReads += 1
        return response({
          id: 'manual-restored',
          workflow_type: 'manual_review',
          store_id: 'store-1',
          start_date: null,
          end_date: null,
          status: 'completed',
          quality_status: 'normal',
          current_step: 'completed',
          attempt_count: 1,
          candidates_ready: false,
          error_code: null,
        })
      }
      throw new Error(`unexpected request: ${path}`)
    })

    const { wrapper } = await mountProposal()
    await flushPromises()
    expect(wrapper.get('[data-test="manual-processing"]').text()).toContain('人工复核处理中')
    expect(wrapper.find('[data-test="submit-manual"]').exists()).toBe(false)

    await vi.advanceTimersByTimeAsync(2_000)
    await flushPromises()
    expect(workflowReads).toBe(1)
    expect(proposalReads).toBe(2)
    wrapper.unmount()
  })

  it('polls two successive manual workflows during one page mount', async () => {
    vi.useFakeTimers()
    setMobile(false)
    let proposalReads = 0
    let manualWrites = 0
    const workflowReads: string[] = []
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/proposals/proposal-1' && !init?.method) {
        proposalReads += 1
        return response(proposalDetail())
      }
      if (path === '/proposals/proposal-1/manual-revision') {
        manualWrites += 1
        return response(
          {
            revision_id: `revision-${manualWrites + 2}`,
            manual_review_workflow_run_id: `manual-${manualWrites}`,
            status: 'accepted',
          },
          202,
        )
      }
      if (path.startsWith('/workflow-runs/manual-')) {
        const id = path.split('/').at(-1)!
        workflowReads.push(id)
        return response({
          id,
          workflow_type: 'manual_review',
          store_id: 'store-1',
          start_date: null,
          end_date: null,
          status: 'completed',
          quality_status: 'normal',
          current_step: 'completed',
          attempt_count: 1,
          candidates_ready: false,
          error_code: null,
        })
      }
      throw new Error(`unexpected request: ${path}`)
    })

    const { wrapper } = await mountProposal()
    await flushPromises()
    await wrapper.get('[data-test="submit-manual"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2_000)
    await flushPromises()
    expect(workflowReads).toEqual(['manual-1'])

    await wrapper.get('[data-test="submit-manual"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2_000)
    await flushPromises()
    expect(workflowReads).toEqual(['manual-1', 'manual-2'])
    expect(proposalReads).toBe(3)
    wrapper.unmount()
  })

  it('keeps an unsupported title readonly and out of the manual change request', async () => {
    setMobile(false)
    const current = proposalDetail()
    current.current_revision!.proposal_output.changes[0]!.evidence = []
    let body: Record<string, unknown> | null = null
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/proposals/proposal-1' && !init?.method) return response(current)
      if (path === '/proposals/proposal-1/manual-revision') {
        body = JSON.parse(String(init?.body)) as Record<string, unknown>
        return response(
          {
            revision_id: 'revision-3',
            manual_review_workflow_run_id: 'manual-1',
            status: 'accepted',
          },
          202,
        )
      }
      throw new Error(`unexpected request: ${path}`)
    })

    const { wrapper } = await mountProposal()
    await flushPromises()
    expect(wrapper.get('[data-test="manual-title"] input').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-test="unsupported-title"]').text()).toContain('缺少可信元数据')
    await wrapper.get('[data-test="submit-manual"]').trigger('click')
    await flushPromises()

    expect(body).toMatchObject({ title: '父版本标题' })
    expect((body!['changes'] as Array<{ field: string }>).map((item) => item.field)).not.toContain(
      'title',
    )
    expect(body).not.toHaveProperty('citations')
    expect(body).not.toHaveProperty('price_suggestions')
    expect(body).not.toHaveProperty('sku_suggestions')
    wrapper.unmount()
  })

  it('keeps existing details visible when a refresh fails and offers retry', async () => {
    vi.useFakeTimers()
    setMobile(false)
    let proposalReads = 0
    vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/proposals/proposal-1') {
        proposalReads += 1
        if (proposalReads === 2) {
          return response({ detail: { code: 'PROPOSAL_READ_FAILED' } }, 503)
        }
        const current = proposalDetail()
        if (proposalReads === 1) {
          current.active_manual_review = {
            manual_review_run_id: 'manual-run-restored',
            workflow_run_id: 'manual-restored',
            proposal_revision_id: 'revision-2',
            status: 'processing',
            quality_status: 'normal',
            current_step: 'semantic_review',
            error_code: null,
          }
        }
        return response(current)
      }
      if (path === '/workflow-runs/manual-restored') {
        return response({
          id: 'manual-restored',
          workflow_type: 'manual_review',
          store_id: 'store-1',
          start_date: null,
          end_date: null,
          status: 'completed',
          quality_status: 'normal',
          current_step: 'completed',
          attempt_count: 1,
          candidates_ready: false,
          error_code: null,
        })
      }
      throw new Error(`unexpected request: ${path}`)
    })

    const { wrapper } = await mountProposal()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2_000)
    await flushPromises()

    expect(wrapper.get('[data-test="proposal-diff"]').text()).toContain('父版本标题')
    expect(wrapper.get('[data-test="proposal-refresh-error"]').text()).toContain('方案加载失败')
    expect(wrapper.find('[data-test="retry-proposal"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('lets a mobile supervisor approve, reloads facts, and shows local publish boundaries', async () => {
    setMobile(true)
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm')
    let proposalReads = 0
    let approveWrites = 0
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/proposals/proposal-1' && !init?.method) {
        proposalReads += 1
        const current = proposalDetail(proposalReads === 1 ? 'pending_approval' : 'completed')
        if (proposalReads > 1) {
          current.publish_record = {
            id: 'publish-1',
            proposal_id: 'proposal-1',
            proposal_revision_id: 'revision-2',
            product_id: 'product-1',
            store_id: 'store-1',
            approved_by: 'supervisor-1',
            approval_action_id: 'action-1',
            before_snapshot: {
              title: '原商品标题',
              selling_points: ['原卖点'],
              description: '原始详情',
              search_keywords: ['原关键词'],
              attributes: { 材质: '棉' },
              current_version: 7,
            },
            after_snapshot: {
              title: '父版本标题',
              selling_points: ['父版本卖点'],
              description: '商品详情\n父版本详情',
              search_keywords: ['父版本关键词'],
              attributes: { 材质: '精梳棉' },
              current_version: 8,
            },
            base_product_version: 7,
            published_product_version: 8,
            published_at: '2026-09-03T09:00:00Z',
          }
        }
        return response(current)
      }
      if (path === '/approvals/proposal-1/approve') {
        approveWrites += 1
        return response({ id: 'publish-1' }, 201)
      }
      throw new Error(`unexpected request: ${path}`)
    })

    const { wrapper } = await mountProposal('supervisor')
    await flushPromises()
    expect(wrapper.find('[data-test="submit-manual"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="submit-proposal"]').exists()).toBe(false)
    await wrapper.get('[data-test="approve-action"]').trigger('click')
    await flushPromises()

    expect(approveWrites).toBe(1)
    expect(proposalReads).toBe(2)
    expect(wrapper.get('[data-test="publish-record"]').text()).toContain('本地模拟')
    expect(wrapper.get('[data-test="publish-record"]').text()).toContain('7 → 8')
    expect(wrapper.get('[data-test="publish-record"]').text()).toContain(
      '价格、SKU、库存与真实平台均未变化',
    )
    expect(wrapper.get('[data-test="publish-before"]').text()).toContain('原商品标题')
    expect(wrapper.get('[data-test="publish-after"]').text()).toContain('父版本标题')
    wrapper.unmount()
  })
})
