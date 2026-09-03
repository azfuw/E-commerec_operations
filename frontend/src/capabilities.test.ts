import { describe, expect, it } from 'vitest'

import {
  canApprove,
  canEditProposal,
  canSelectProduct,
  canStartAnalysis,
  canSubmitProposal,
} from './capabilities'
import type { UserRole } from './types'

describe('responsive capabilities', () => {
  it.each(['operator', 'supervisor', 'admin'] as UserRole[])(
    'enforces desktop and mobile actions for %s',
    (role) => {
      for (const mobile of [false, true]) {
        expect(canStartAnalysis(role, mobile)).toBe(role === 'operator' && !mobile)
        expect(canSelectProduct(role, mobile)).toBe(role === 'operator' && !mobile)
        expect(canEditProposal(role, 'pending_manual', mobile)).toBe(!mobile)
        expect(canSubmitProposal(role, 'draft_ready', mobile)).toBe(!mobile)
        expect(canApprove(role, 'pending_approval', mobile)).toBe(role !== 'operator')
      }
    },
  )

  it('allows supervisor and admin self-approval only while pending approval', () => {
    expect(canApprove('operator', 'pending_approval', false)).toBe(false)
    expect(canApprove('supervisor', 'pending_approval', true)).toBe(true)
    expect(canApprove('admin', 'pending_approval', false)).toBe(true)
    expect(canApprove('supervisor', 'draft_ready', false)).toBe(false)
  })
})
