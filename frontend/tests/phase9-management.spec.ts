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

test('mobile management deep links perform zero management requests',async ({page}) => {
  await page.setViewportSize({width:390,height:844})
  await page.addInitScript(() => sessionStorage.setItem('access_token','fixture'))
  await page.route('**/auth/me',route => route.fulfill({json:{id:'admin',username:'admin',role:'admin'}}))
  let requests = 0
  await page.route(/\/((knowledge|admin|agent-evaluations)\/|agent-calls\?|audit-events\?)/,route => { requests++; return route.abort() })
  for (const path of ['knowledge','agent-evaluations','audit-events','admin']) {
    await page.goto(`/app/${path}`)
    await expect(page.getByRole('heading',{name:'请使用桌面或平板访问管理模块'})).toBeVisible()
  }
  expect(requests).toBe(0)
})

test('admin edits users and exact scopes, confirms store state and refetches facts', async ({page}) => {
  await page.setViewportSize({width:1024,height:768})
  await page.addInitScript(() => sessionStorage.setItem('access_token','fixture'))
  await page.route('**/auth/me', route => route.fulfill({json:{id:'admin',username:'admin',role:'admin'}}))
  const users = [
    {id:'admin',username:'admin',role:'admin',status:'active',store_ids:[],created_at:'2026-09-05T00:00:00Z'},
    {id:'operator',username:'operator',role:'operator',status:'active',store_ids:['s1'],created_at:'2026-09-05T00:00:00Z'},
  ]
  const store = {id:'s2',name:'测试店铺',code:'store-code',enabled:true,created_at:'2026-09-05T00:00:00Z'}
  let reads = 0, writes = 0
  await page.route('**/admin/users?*', route => { reads++; return route.fulfill({json:{items:users,total:2}}) })
  await page.route('**/admin/users/operator', route => {
    expect(route.request().method()).toBe('PATCH')
    Object.assign(users[1]!,route.request().postDataJSON()); writes++
    return route.fulfill({json:users[1]})
  })
  await page.route('**/admin/users/operator/store-scopes', route => {
    expect(route.request().method()).toBe('PUT')
    expect(route.request().postDataJSON()).toEqual({store_ids:['s2']})
    users[1]!.store_ids = ['s2']; writes++
    return route.fulfill({json:users[1]})
  })
  await page.route('**/admin/stores?*', route => { reads++; return route.fulfill({json:{items:[store],total:1}}) })
  await page.route('**/admin/stores/s2', route => {
    expect(route.request().postDataJSON()).toEqual({enabled:false})
    store.enabled=false; writes++; return route.fulfill({json:store})
  })
  await page.goto('/app/admin')
  await expect(page.getByRole('link',{name:'系统管理'})).toBeVisible()
  await page.getByRole('button',{name:'编辑用户',exact:true}).first().click()
  await expect(page.getByLabel('用户角色',{exact:true})).toBeDisabled()
  await expect(page.getByLabel('用户状态',{exact:true}).last()).toBeDisabled()
  await page.getByRole('button',{name:'Close this dialog'}).click()
  await page.getByRole('button',{name:'编辑用户',exact:true}).nth(1).click()
  await page.getByLabel('用户角色',{exact:true}).selectOption('supervisor')
  await page.getByRole('button',{name:'保存变更'}).click()
  await expect(page.getByText('变更已保存',{exact:true})).toBeVisible()
  await expect(page.getByRole('cell',{name:'主管',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'店铺权限',exact:true}).nth(1).click()
  await page.getByLabel('授权店铺 ID（每行一个）').fill('s2')
  await page.getByRole('button',{name:'保存变更'}).click()
  await expect(page.getByRole('cell',{name:'s2',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'编辑用户',exact:true}).nth(1).click()
  await page.getByLabel('用户状态',{exact:true}).last().selectOption('disabled')
  await page.getByRole('button',{name:'保存变更'}).click()
  await expect(page.getByRole('button',{name:'保存变更'})).toBeDisabled()
  expect(writes).toBe(2)
  await page.getByRole('button',{name:'取消',exact:true}).click()
  expect(writes).toBe(2)
  await page.getByRole('button',{name:'保存变更'}).click()
  await page.getByRole('button',{name:'确认',exact:true}).click()
  await expect(page.getByRole('cell',{name:'停用',exact:true})).toBeVisible()
  await page.getByRole('tab',{name:'店铺启停'}).click()
  await page.getByRole('button',{name:'停用店铺'}).click()
  expect(writes).toBe(3)
  await page.getByRole('button',{name:'确认',exact:true}).click()
  await expect(page.getByRole('button',{name:'启用店铺'})).toBeVisible()
  expect(writes).toBe(4)
  expect(reads).toBe(6)
  await expect(page.getByRole('dialog',{name:'确认变更'})).not.toBeVisible()
  await page.screenshot({path:'D:/E-commerce_operations_env/phase9-system-tablet.png',fullPage:true})
})

test('mobile operator sees forbidden for role restricted management routes', async ({page}) => {
  await page.setViewportSize({width:390,height:844})
  await page.addInitScript(() => sessionStorage.setItem('access_token','fixture'))
  await page.route('**/auth/me', route => route.fulfill({json:{id:'op',username:'op',role:'operator'}}))
  let requests=0
  await page.route(/\/(admin\/|agent-evaluations\/|agent-calls\?|audit-events\?)/,route=>{requests++;return route.abort()})
  for(const path of ['agent-evaluations','audit-events','admin']) {
    await page.goto(`/app/${path}`)
    await expect(page).toHaveURL(/\/app\/forbidden$/)
  }
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
