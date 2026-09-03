import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import WorkbenchPage from './WorkbenchPage.vue'
import type { StoreSummary, WorkbenchTask, WorkbenchTaskList } from '../types'

const stores: StoreSummary[] = [
  { id: 'store-1', code: 'MAIN', name: '旗舰店' },
]

const tasks: WorkbenchTask[] = [
  {
    id: 'proposal-1',
    kind: 'proposal',
    store_id: 'store-1',
    product_id: 'product-1',
    analysis_run_id: 'analysis-source-1',
    proposal_id: 'proposal-1',
    workflow_run_id: 'optimization-1',
    workflow_type: 'optimization',
    status: 'pending_approval',
    quality_status: 'normal',
    current_step: 'approval_pending',
    action_required: 'review_approval',
    requires_current_user_action: true,
    created_by: 'operator-1',
    updated_at: '2026-09-03T08:00:00Z',
  },
  {
    id: 'proposal-2',
    kind: 'proposal',
    store_id: 'store-1',
    product_id: 'product-2',
    analysis_run_id: 'analysis-source-2',
    proposal_id: 'proposal-2',
    workflow_run_id: 'manual-1',
    workflow_type: 'manual_review',
    status: 'pending_manual',
    quality_status: 'degraded',
    current_step: 'manual_review_pending',
    action_required: 'edit_proposal',
    requires_current_user_action: true,
    created_by: 'operator-1',
    updated_at: '2026-09-03T07:00:00Z',
  },
  {
    id: 'analysis-1',
    kind: 'analysis',
    store_id: 'store-1',
    product_id: null,
    analysis_run_id: 'analysis-1',
    proposal_id: null,
    workflow_run_id: 'analysis-1',
    workflow_type: 'analysis',
    status: 'processing',
    quality_status: 'partial',
    current_step: 'rank',
    action_required: 'wait',
    requires_current_user_action: false,
    created_by: 'operator-1',
    updated_at: '2026-09-03T06:00:00Z',
  },
  {
    id: 'analysis-2',
    kind: 'analysis',
    store_id: 'store-1',
    product_id: null,
    analysis_run_id: 'analysis-2',
    proposal_id: null,
    workflow_run_id: 'analysis-2',
    workflow_type: 'analysis',
    status: 'completed',
    quality_status: 'normal',
    current_step: 'completed',
    action_required: 'view_result',
    requires_current_user_action: false,
    created_by: 'operator-1',
    updated_at: '2026-09-03T05:00:00Z',
  },
]

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function taskList(items: WorkbenchTask[] = tasks): WorkbenchTaskList {
  return { items, page: 1, page_size: 20, total: items.length }
}

function installFetch(
  loadTasks: (url: URL) => Promise<Response> = async () => response(taskList()),
): string[] {
  const requests: string[] = []
  vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
    const path = String(input)
    requests.push(path)
    const url = new URL(path, 'http://test')
    if (url.pathname === '/stores') return response(stores)
    if (url.pathname === '/workbench/tasks') return loadTasks(url)
    throw new Error(`unexpected request: ${url.pathname}`)
  })
  return requests
}

async function mountWorkbench(path = '/app/workbench') {
  const router = createRouter({
    history: createMemoryHistory('/app/'),
    routes: [
      { path: '/workbench', name: 'workbench', component: WorkbenchPage },
      { path: '/proposals', name: 'proposals', component: WorkbenchPage },
    ],
  })
  await router.push(path.replace('/app', ''))
  await router.isReady()
  const wrapper = mount(WorkbenchPage, {
    global: { plugins: [ElementPlus, router] },
  })
  return { router, wrapper }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('WorkbenchPage', () => {
  it('shows loading then the current-page summary, desktop table, and mobile rows', async () => {
    let finish!: (value: Response) => void
    installFetch(() => new Promise<Response>((resolve) => (finish = resolve)))
    const { wrapper } = await mountWorkbench()

    expect(wrapper.get('[data-test="workbench-loading"]').text()).toContain('加载')
    finish(response(taskList()))
    await flushPromises()

    expect(wrapper.get('[data-test="needs-action-count"]').text()).toContain('2')
    expect(wrapper.get('[data-test="summary-label"]').text()).toContain('当前页')
    expect(wrapper.get('[data-test="task-proposal-1"]').text()).toContain('等待审批')
    expect(wrapper.get('[data-test="task-proposal-1"]').text()).toContain('质量正常')
    expect(wrapper.get('[data-test="task-proposal-2"]').text()).toContain('降级')
    expect(wrapper.get('[data-test="task-analysis-1"]').text()).toContain('部分结果')
    expect(wrapper.get('[data-test="continue-proposal-1"]').attributes('href')).toBe(
      '/app/proposals/proposal-1',
    )
    expect(wrapper.get('[data-test="mobile-task-proposal-1"]').text()).toContain(
      '旗舰店',
    )
    expect(wrapper.get('[data-test="mobile-task-proposal-1"]').text()).toContain(
      '审核方案',
    )
  })

  it('distinguishes a system empty state from a filtered empty state', async () => {
    let result = taskList([])
    installFetch(async () => response(result))
    const { wrapper } = await mountWorkbench()
    await flushPromises()

    expect(wrapper.get('[data-test="system-empty"]').text()).toContain('当前没有任务')

    result = taskList([])
    wrapper.getComponent('[data-test="status-filter"]').vm.$emit(
      'update:modelValue',
      'failed',
    )
    await flushPromises()

    expect(wrapper.get('[data-test="filtered-empty"]').text()).toContain(
      '没有符合筛选条件的任务',
    )
  })

  it('keeps filters when retrying a safe API error', async () => {
    const taskQueries: URL[] = []
    let fail = false
    installFetch(async (url) => {
      taskQueries.push(url)
      if (fail) {
        fail = false
        return response({ detail: { code: 'WORKBENCH_READ_FAILED' } }, 503)
      }
      return response(taskList())
    })
    const { wrapper } = await mountWorkbench()
    await flushPromises()

    fail = true
    wrapper.getComponent('[data-test="store-filter"]').vm.$emit(
      'update:modelValue',
      'store-1',
    )
    await flushPromises()
    expect(wrapper.get('[data-test="workbench-error"]').text()).toContain(
      '任务加载失败，请重试',
    )

    await wrapper.get('[data-test="retry-workbench"]').trigger('click')
    await flushPromises()

    expect(taskQueries.at(-2)?.searchParams.get('store_id')).toBe('store-1')
    expect(taskQueries.at(-1)?.searchParams.get('store_id')).toBe('store-1')
    expect(wrapper.find('[data-test="workbench-error"]').exists()).toBe(false)
  })

  it('resets pagination when filters change', async () => {
    const taskQueries: URL[] = []
    installFetch(async (url) => {
      taskQueries.push(url)
      return response(taskList())
    })
    const { wrapper } = await mountWorkbench()
    await flushPromises()

    wrapper.getComponent('[data-test="task-pagination"]').vm.$emit('current-change', 2)
    await flushPromises()
    expect(taskQueries.at(-1)?.searchParams.get('page')).toBe('2')

    wrapper.getComponent('[data-test="kind-filter"]').vm.$emit(
      'update:modelValue',
      'analysis',
    )
    await flushPromises()
    expect(taskQueries.at(-1)?.searchParams.get('page')).toBe('1')
    expect(taskQueries.at(-1)?.searchParams.get('kind')).toBe('analysis')
  })

  it('uses the same endpoint with a fixed proposal kind for the proposal view', async () => {
    const taskQueries: URL[] = []
    installFetch(async (url) => {
      taskQueries.push(url)
      return response(taskList(tasks.filter((task) => task.kind === 'proposal')))
    })
    const { wrapper } = await mountWorkbench('/app/proposals')
    await flushPromises()

    expect(wrapper.get('h1').text()).toBe('优化任务')
    expect(taskQueries).toHaveLength(1)
    expect(taskQueries[0]?.searchParams.get('kind')).toBe('proposal')
    expect(wrapper.find('[data-test="kind-filter"]').exists()).toBe(false)
  })
})
