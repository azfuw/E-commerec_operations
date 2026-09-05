import {mount,flushPromises} from '@vue/test-utils'
import ElementPlus from 'element-plus'
import {afterEach,it,expect,vi} from 'vitest'
import {setCurrentUser,clearSession} from '../session'
import AuditEventsPage from './AuditEventsPage.vue'
afterEach(()=>{clearSession();vi.unstubAllGlobals()})
it('blocks mobile audit requests',async()=>{
  setCurrentUser({id:'a',username:'a',role:'admin'});vi.stubGlobal('matchMedia',()=>({matches:true}))
  const fetch=vi.fn();vi.stubGlobal('fetch',fetch)
  const w=mount(AuditEventsPage,{global:{plugins:[ElementPlus]}});await flushPromises()
  expect(fetch).not.toHaveBeenCalled();expect(w.text()).toContain('桌面或平板');w.unmount()
})
it('preserves filters and renders safe errors',async()=>{
  setCurrentUser({id:'a',username:'a',role:'admin'})
  vi.stubGlobal('fetch',async()=>new Response(JSON.stringify({detail:'unsafe'}),{status:503}))
  const w=mount(AuditEventsPage,{global:{plugins:[ElementPlus]}});await flushPromises()
  await w.get('[aria-label="审计店铺 ID"]').setValue('store')
  await w.get('form').trigger('submit');await flushPromises()
  expect((w.get('[aria-label="审计店铺 ID"]').element as HTMLInputElement).value).toBe('store')
  expect(w.find('[role="alert"]').exists()).toBe(true);expect(w.text()).not.toContain('unsafe');w.unmount()
})
