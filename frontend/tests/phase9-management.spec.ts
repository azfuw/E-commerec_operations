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
