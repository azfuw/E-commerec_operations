import { expect, test } from '@playwright/test'

test('desktop knowledge search uses authorized context and safe text', async ({page}) => {
  await page.addInitScript(() => sessionStorage.setItem('access_token','fixture'))
  await page.route('**/auth/me',route => route.fulfill({json:{id:'admin',username:'admin',role:'admin'}}))
  await page.route('**/stores',route => route.fulfill({json:[{id:'store',name:'测试店铺',code:'store'}]}))
  await page.route('**/knowledge/documents?*',route => route.fulfill({json:{data:{items:[],total:0,page:1,page_size:20}}}))
  await page.route('**/knowledge/search',route => {
    expect(route.request().postDataJSON()).toEqual({store_id:'store',query:'规则'})
    return route.fulfill({json:{data:{citations:[{chunk_id:'chunk',document_name:'规则',version_number:1,category:'demo',canonical_text:'<img src=x onerror=alert(1)>',final_score:0.9}]},quality:{status:'ok'}}})
  })
  await page.goto('/app/knowledge')
  await expect(page.getByRole('heading',{name:'知识库',exact:true})).toBeVisible()
  await page.getByLabel('检索店铺').selectOption('store')
  await page.getByLabel('检索内容').fill('规则')
  await page.getByRole('button',{name:'检索',exact:true}).click()
  await expect(page.getByText('<img src=x onerror=alert(1)>',{exact:true})).toBeVisible()
  await expect(page.locator('article img')).toHaveCount(0)
})

test('mobile knowledge deep link performs zero management requests',async ({page}) => {
  await page.setViewportSize({width:390,height:844})
  await page.addInitScript(() => sessionStorage.setItem('access_token','fixture'))
  await page.route('**/auth/me',route => route.fulfill({json:{id:'admin',username:'admin',role:'admin'}}))
  let requests = 0
  await page.route('**/knowledge/**',route => { requests++; return route.abort() })
  await page.goto('/app/knowledge')
  await expect(page.getByRole('heading',{name:'请使用桌面或平板访问管理模块'})).toBeVisible()
  expect(requests).toBe(0)
})

test('tablet supervisor reads evaluation detail and call summary',async ({page})=>{
  await page.setViewportSize({width:1024,height:768})
  await page.addInitScript(()=>sessionStorage.setItem('access_token','fixture'))
  await page.route('**/auth/me',route=>route.fulfill({json:{id:'s',username:'supervisor',role:'supervisor'}}))
  const run={id:'run',agent_type:'analysis',store_id:'store',status:'completed',summary:{total_cases:1,passed_cases:1,failed_cases:0,average_latency_ms:1},suite_version:'v1',runner_version:'v1',dataset_version:'v1',started_at:'2026-09-05T00:00:00Z',completed_at:'2026-09-05T00:00:01Z'}
  await page.route('**/agent-evaluations/runs?*',route=>route.fulfill({json:{items:[run],total:1}}))
  await page.route('**/agent-evaluations/runs/run',route=>route.fulfill({json:{...run,results:[{case_key:'exact',case_version:1,outcome:'passed',result_code:'EVALUATION_PASSED',latency_ms:1,metrics:{candidate_set_valid:true}}]}}))
  await page.route('**/agent-calls?*',route=>route.fulfill({json:{items:[],total:0}}))
  await page.goto('/app/agent-evaluations')
  await page.getByRole('button',{name:'查看结果'}).click()
  await expect(page.getByText('EVALUATION_PASSED',{exact:true})).toBeVisible()
  await page.getByRole('button',{name:'Close this dialog'}).click()
  await page.getByRole('tab',{name:'Agent 调用'}).click()
  await expect(page.getByText('暂无调用记录')).toBeVisible()
})

test('desktop admin can filter audit and inspect safe detail',async({page})=>{
  await page.addInitScript(()=>sessionStorage.setItem('access_token','fixture'))
  await page.route('**/auth/me',route=>route.fulfill({json:{id:'a',username:'admin',role:'admin'}}))
  await page.route('**/audit-events?*',route=>route.fulfill({json:{items:[{id:'audit',event_type:'admin_store_updated',outcome:'success',store_id:'store',details:{store_enabled:false},created_at:'2026-09-05T00:00:00Z'}],total:1}}))
  await page.goto('/app/audit-events')
  await page.getByRole('button',{name:'查看详情'}).click()
  await expect(page.getByRole('heading',{name:'操作详情'})).toBeVisible()
  await expect(page.getByText('store_enabled',{exact:true})).toBeVisible()
})
