import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import LoginPage from './LoginPage.vue'

const apiMock = vi.hoisted(() => ({
  login: (_username: string, _password: string): Promise<void> => Promise.resolve(),
}))

vi.mock('../api', () => ({
  login: (username: string, password: string) => apiMock.login(username, password),
}))

async function mountLogin(query = '') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/app/login', name: 'login', component: { template: '<div />' } },
      { path: '/app/logistics', name: 'logistics', component: { template: '<div />' } },
      {
        path: '/app/workbench',
        name: 'workbench',
        component: { template: '<div />' },
      },
    ],
  })
  await router.push('/app/login' + query)
  await router.isReady()
  const wrapper = mount(LoginPage, { global: { plugins: [ElementPlus, router] } })
  return { router, wrapper }
}

describe('LoginPage', () => {
  beforeEach(() => {
    apiMock.login = () => Promise.resolve()
  })

  it('uses visible labels and disables duplicate submission', async () => {
    let finishLogin!: () => void
    apiMock.login = () => new Promise<void>((resolve) => (finishLogin = resolve))
    const { wrapper } = await mountLogin()

    expect(wrapper.get('label[for="username"]').text()).toBe('用户名')
    expect(wrapper.get('label[for="password"]').text()).toBe('密码')
    await wrapper.get('input#username').setValue('operator')
    await wrapper.get('input#password').setValue('DemoPass!2026')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    finishLogin()
    await flushPromises()
  })

  it('shows one safe login error', async () => {
    apiMock.login = async () => {
      throw new Error('provider detail')
    }
    const { wrapper } = await mountLogin()

    await wrapper.get('input#username').setValue('operator')
    await wrapper.get('input#password').setValue('wrong-password')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('登录失败，请检查账号或密码')
    expect(wrapper.text()).not.toContain('provider detail')
  })

  it('enters the protected workbench after login', async () => {
    const { router, wrapper } = await mountLogin()

    await wrapper.get('input#username').setValue('operator')
    await wrapper.get('input#password').setValue('DemoPass!2026')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/app/workbench')
  })

  it('returns a logistics visitor to the logistics workspace', async () => {
    const { router, wrapper } = await mountLogin('?next=logistics')
    await wrapper.get('input#username').setValue('logistics')
    await wrapper.get('input#password').setValue('Logistics!2026')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/app/logistics')
  })
})
