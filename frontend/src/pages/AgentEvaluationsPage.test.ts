import { mount,flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach,it,expect,vi } from 'vitest'
import AgentEvaluationsPage from './AgentEvaluationsPage.vue'
import {setCurrentUser,clearSession} from '../session'
import {canViewAgentObservability} from '../capabilities'
afterEach(() => {clearSession();vi.unstubAllGlobals()})
it('rejects operator and all mobile loads before fetch',async () => {
  const fetch=vi.fn();vi.stubGlobal('fetch',fetch)
  for(const role of ['operator','supervisor','admin'] as const){
    vi.stubGlobal('matchMedia',()=>({matches:role!=='operator'}))
    setCurrentUser({id:'u',username:'u',role})
    expect(canViewAgentObservability(role,role!=='operator')).toBe(false)
    const w=mount(AgentEvaluationsPage,{global:{plugins:[ElementPlus]}});await flushPromises()
    expect(fetch).not.toHaveBeenCalled();w.unmount()
  }
})
it('renders empty evidence with no evaluation start control',async()=>{
  setCurrentUser({id:'a',username:'a',role:'admin'})
  vi.stubGlobal('fetch',async()=>new Response(JSON.stringify({items:[],total:0})))
  const w=mount(AgentEvaluationsPage,{global:{plugins:[ElementPlus]}});await flushPromises()
  expect(w.text()).toContain('暂无评测记录');expect(w.text()).not.toContain('开始评测');w.unmount()
})
it.each([401,403,404,422,503])('safe load failure %s',async status=>{
  setCurrentUser({id:'a',username:'a',role:'admin'})
  vi.stubGlobal('fetch',async()=>new Response(JSON.stringify({detail:'sensitive'}),{status}))
  const w=mount(AgentEvaluationsPage,{global:{plugins:[ElementPlus]}});await flushPromises()
  expect(w.find('[role="alert"]').exists()).toBe(true);expect(w.text()).not.toContain('sensitive');w.unmount()
})
