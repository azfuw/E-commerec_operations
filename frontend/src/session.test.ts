import { beforeEach, describe, expect, it } from 'vitest'

import { clearSession, session, setCurrentUser, setToken } from './session'

describe('session', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    clearSession()
  })

  it('stores the bearer token only for the browser session', () => {
    setToken('session-token')

    expect(session.token).toBe('session-token')
    expect(sessionStorage.getItem('access_token')).toBe('session-token')
    expect(localStorage.getItem('access_token')).toBeNull()
  })

  it('clears the token and database identity together', () => {
    setToken('session-token')
    setCurrentUser({ id: 'operator-1', username: 'operator', role: 'operator' })

    clearSession()

    expect(session).toMatchObject({ token: null, user: null })
    expect(sessionStorage.getItem('access_token')).toBeNull()
  })
})
