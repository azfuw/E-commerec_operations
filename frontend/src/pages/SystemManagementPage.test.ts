import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, expect, it, vi } from 'vitest'
import SystemManagementPage from './SystemManagementPage.vue'
import { canManageSystem } from '../capabilities'
import { setCurrentUser, clearSession } from '../session'

afterEach(() => { clearSession(); vi.unstubAllGlobals() })
it.each(['operator', 'supervisor', 'admin'] as const)('enforces admin and device access for %s', async role => {
  for (const mobile of [true, false]) {
    expect(canManageSystem(role, mobile)).toBe(role === 'admin' && !mobile)
    if (role === 'admin' && !mobile) continue
    setCurrentUser({ id: 'a', username: 'a', role, department: 'operations' })
    vi.stubGlobal('matchMedia', () => ({ matches: mobile }))
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    const wrapper = mount(SystemManagementPage, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(fetch).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain(role === 'admin' ? '桌面或平板' : '没有访问权限')
    wrapper.unmount()
  }
})
it.each([401, 403, 404, 409, 422, 503])('renders safe error %s', async status => {
  setCurrentUser({ id: 'a', username: 'a', role: 'admin', department: 'operations' })
  vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ detail: 'private diagnostic' }), { status }))
  const wrapper = mount(SystemManagementPage, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  expect(wrapper.find('[role="alert"]').exists()).toBe(true)
  expect(wrapper.text()).not.toContain('private diagnostic')
  wrapper.unmount()
})
it('distinguishes empty lists from filter misses and preserves filter values', async () => {
  setCurrentUser({ id: 'a', username: 'a', role: 'admin', department: 'operations' })
  const fetch = vi.fn(async () => new Response(JSON.stringify({ items: [], total: 0 })))
  vi.stubGlobal('fetch', fetch)
  const wrapper = mount(SystemManagementPage, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  expect(wrapper.text()).toContain('暂无用户')
  await wrapper.get('[aria-label="用户角色筛选"]').setValue('operator')
  await wrapper.get('form').trigger('submit'); await flushPromises()
  expect(wrapper.text()).toContain('没有符合筛选条件的用户')
  expect(String(fetch.mock.calls.at(-1)?.[0])).toContain('role=operator')
  wrapper.unmount()
})
