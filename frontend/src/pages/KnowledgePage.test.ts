import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, expect, it, vi } from 'vitest'
import KnowledgePage from './KnowledgePage.vue'
import { setCurrentUser, clearSession } from '../session'
import { canUseKnowledge } from '../capabilities'

afterEach(() => { clearSession(); vi.unstubAllGlobals() })
it('blocks every mobile role before requests', async () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true }))
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
  for (const role of ['operator','supervisor','admin'] as const) {
    expect(canUseKnowledge(role,'operations',true)).toBe(false)
    setCurrentUser({id:'u',username:'u',role,department:'operations'})
    const wrapper = mount(KnowledgePage,{global:{plugins:[ElementPlus]}})
    await flushPromises(); expect(wrapper.text()).toContain('桌面或平板')
    expect(fetch).not.toHaveBeenCalled(); wrapper.unmount()
  }
})
it('operator can search but has no document management requests', async () => {
  setCurrentUser({id:'u',username:'u',role:'operator',department:'operations'})
  const fetch = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify([{id:'s',name:'Store',code:'s'}])))
  vi.stubGlobal('fetch',fetch)
  const wrapper = mount(KnowledgePage,{global:{plugins:[ElementPlus]}})
  await flushPromises(); expect(fetch).toHaveBeenCalledTimes(1)
  expect(String(fetch.mock.calls[0]?.[0])).toBe('/stores')
  expect(wrapper.text()).not.toContain('上传文档'); wrapper.unmount()
})
it.each([401,403,404,409,422,503])('shows safe server error %s',async status => {
  setCurrentUser({id:'a',username:'a',role:'admin',department:'operations'})
  vi.stubGlobal('fetch',async () => new Response(JSON.stringify({detail:'private diagnostic'}),{status}))
  const wrapper = mount(KnowledgePage,{global:{plugins:[ElementPlus]}})
  await flushPromises(); expect(wrapper.text()).not.toContain('private diagnostic')
  expect(wrapper.find('[role="alert"]').exists()).toBe(true); wrapper.unmount()
})
