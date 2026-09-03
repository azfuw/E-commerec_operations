import { reactive } from 'vue'

import type { CurrentUser } from './types'

const TOKEN_KEY = 'access_token'

export type SessionState = {
  token: string | null
  user: CurrentUser | null
  ready: boolean
}

export const session: SessionState = reactive({
  token: sessionStorage.getItem(TOKEN_KEY),
  user: null,
  ready: false,
})

export function setToken(token: string): void {
  session.token = token
  sessionStorage.setItem(TOKEN_KEY, token)
}

export function setCurrentUser(user: CurrentUser): void {
  session.user = user
}

export function clearSession(): void {
  sessionStorage.removeItem(TOKEN_KEY)
  session.token = null
  session.user = null
  session.ready = true
}
