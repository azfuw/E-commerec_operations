import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { clearSession, setCurrentUser } from '../session'
import ApprovalsPage from './ApprovalsPage.vue'

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

async function mountPage() {
  setCurrentUser({ id: 'supervisor-1', username: 'supervisor', role: 'supervisor' })
  const router = createRouter({
    history: createMemoryHistory('/app/'),
    routes: [
      { path: '/approvals', component: ApprovalsPage },
      { path: '/proposals/:proposalId', name: 'proposal', component: { template: '<div />' } },
    ],
  })
  await router.push('/approvals')
  await router.isReady()
  return {
    router,
    wrapper: mount(ApprovalsPage, { global: { plugins: [ElementPlus, router] } }),
  }
}

afterEach(() => {
  clearSession()
  vi.unstubAllGlobals()
})

describe('ApprovalsPage', () => {
  it('shows scoped approvals, pagination, and opens the proposal', async () => {
    const paths: string[] = []
    vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
      paths.push(String(input))
      return response({
        items: [
          {
            proposal_id: 'proposal-1',
            proposal_revision_id: 'revision-2',
            revision_number: 2,
            store_id: 'store-1',
            product_id: 'product-1',
            submitted_by: 'supervisor-1',
            status: 'pending_approval',
            submitted_at: '2026-09-03T08:00:00Z',
          },
        ],
        page: 1,
        page_size: 20,
        total: 21,
      })
    })

    const { router, wrapper } = await mountPage()
    await flushPromises()
    expect(paths).toEqual(['/approvals?page=1&page_size=20'])
    expect(wrapper.get('[data-test="approval-proposal-1"]').text()).toContain('product-1')
    expect(wrapper.get('[data-test="approval-total"]').text()).toContain('21')
    await wrapper.get('[data-test="approvals-next"]').trigger('click')
    await flushPromises()
    expect(paths.at(-1)).toBe('/approvals?page=2&page_size=20')
    await wrapper.get('[data-test="open-proposal-1"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/proposals/proposal-1')
    wrapper.unmount()
  })

  it('shows an explicit empty state', async () => {
    vi.stubGlobal('fetch', async () =>
      response({ items: [], page: 1, page_size: 20, total: 0 }),
    )
    const { wrapper } = await mountPage()
    await flushPromises()
    expect(wrapper.get('[data-test="approvals-empty"]').text()).toContain('暂无待审批方案')
    wrapper.unmount()
  })

  it('shows a safe forbidden error without stale rows', async () => {
    vi.stubGlobal('fetch', async () => response({ detail: { code: 'APPROVAL_FORBIDDEN' } }, 403))
    const { wrapper } = await mountPage()
    await flushPromises()
    expect(wrapper.get('[data-test="approvals-error"]').text()).toContain('无权查看待审批方案')
    expect(wrapper.find('[data-test^="approval-proposal-"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
