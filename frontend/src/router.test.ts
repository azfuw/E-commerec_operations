import { afterEach, describe, expect, it } from 'vitest'

import { createAppRouter } from './router'
import { clearSession, setCurrentUser } from './session'
import type { CurrentUser } from './types'

const user = (role: CurrentUser['role'], department: CurrentUser['department']): CurrentUser => ({
  id: `${department}-${role}`,
  username: `${department}-${role}`,
  role,
  department,
})

afterEach(() => clearSession())

describe('department route isolation', () => {
  it.each([
    [user('operator', 'operations'), '/logistics?view=returns'],
    [user('supervisor', 'logistics'), '/workbench'],
    [user('supervisor', 'logistics'), '/knowledge'],
    [user('supervisor', 'logistics'), '/audit-events'],
    [user('supervisor', 'logistics'), '/agent-evaluations'],
  ] as const)('blocks %s from mounting %s', async (currentUser, path) => {
    setCurrentUser(currentUser)
    const router = createAppRouter()
    await router.push(path)
    expect(router.currentRoute.value.name).toBe('forbidden')
  })

  it('lands a logistics account in logistics from the root route', async () => {
    setCurrentUser(user('operator', 'logistics'))
    const router = createAppRouter()
    await router.push('/')
    expect(router.currentRoute.value.fullPath).toBe('/logistics')
  })

  it.each(['/workbench', '/logistics?view=returns', '/knowledge', '/admin'])(
    'keeps both department workspaces available to an admin at %s',
    async (path) => {
      setCurrentUser(user('admin', 'operations'))
      const router = createAppRouter()
      await router.push(path)
      expect(router.currentRoute.value.name).not.toBe('forbidden')
    },
  )
})
