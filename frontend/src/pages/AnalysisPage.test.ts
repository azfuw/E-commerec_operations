import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElDatePicker } from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { clearSession, setCurrentUser } from '../session'
import AnalysisPage from './AnalysisPage.vue'

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

async function mountAnalysis() {
  setCurrentUser({ id: 'operator-1', username: 'operator', role: 'operator', department: 'operations' })
  const router = createRouter({
    history: createMemoryHistory('/app/'),
    routes: [
      { path: '/analysis', name: 'analysis', component: AnalysisPage },
      { path: '/analysis/:runId', name: 'analysis-run', component: { template: '<div />' } },
    ],
  })
  await router.push('/analysis')
  await router.isReady()
  const wrapper = mount(AnalysisPage, { global: { plugins: [ElementPlus, router] } })
  await flushPromises()
  return { router, wrapper }
}

afterEach(() => {
  clearSession()
  vi.unstubAllGlobals()
})

describe('AnalysisPage', () => {
  it('accepts inclusive one-day and ninety-day ranges but disables ninety-one days', async () => {
    vi.stubGlobal('fetch', async () =>
      response([{ id: 'store-1', code: 'MAIN', name: '旗舰店' }]),
    )
    const { wrapper } = await mountAnalysis()
    wrapper.getComponent('[data-test="analysis-store"]').vm.$emit(
      'update:modelValue',
      'store-1',
    )

    wrapper.getComponent(ElDatePicker).vm.$emit(
      'update:modelValue',
      ['2026-01-01', '2026-01-01'],
    )
    await flushPromises()
    expect(wrapper.get('[data-test="start-analysis"]').attributes('disabled')).toBeUndefined()

    wrapper.getComponent(ElDatePicker).vm.$emit(
      'update:modelValue',
      ['2026-01-01', '2026-03-31'],
    )
    await flushPromises()
    expect(wrapper.get('[data-test="start-analysis"]').attributes('disabled')).toBeUndefined()

    wrapper.getComponent(ElDatePicker).vm.$emit(
      'update:modelValue',
      ['2026-01-01', '2026-04-01'],
    )
    await flushPromises()
    expect(wrapper.get('[data-test="start-analysis"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-test="date-error"]').text()).toContain('1 至 90 天')
  })

  it('sends unchanged YYYY-MM-DD values and navigates on the 202 response', async () => {
    let requestBody: unknown
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/stores') {
        return response([{ id: 'store-1', code: 'MAIN', name: '旗舰店' }])
      }
      if (String(input) === '/analysis-runs') {
        requestBody = JSON.parse(String(init?.body))
        return response({ workflow_run_id: 'analysis-1', status: 'accepted' }, 202)
      }
      throw new Error(`unexpected request: ${String(input)}`)
    })
    const { router, wrapper } = await mountAnalysis()
    wrapper.getComponent('[data-test="analysis-store"]').vm.$emit(
      'update:modelValue',
      'store-1',
    )
    wrapper.getComponent(ElDatePicker).vm.$emit(
      'update:modelValue',
      ['2026-08-01', '2026-08-31'],
    )
    await flushPromises()
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(requestBody).toEqual({
      store_id: 'store-1',
      start_date: '2026-08-01',
      end_date: '2026-08-31',
    })
    expect(router.currentRoute.value.fullPath).toBe('/analysis/analysis-1')
  })
})
