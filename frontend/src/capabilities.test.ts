import { describe, expect, it } from 'vitest'

import {
  canApprove,
  canEditProposal,
  canSelectProduct,
  canStartAnalysis,
  canSubmitProposal,
  canUseKnowledge,
  canViewAgentObservability,
  canViewAuditEvents,
} from './capabilities'
import type { UserRole } from './types'

describe('responsive capabilities', () => {
  it('keeps operations capabilities inside operations while admins retain both departments', () => {
    expect(canStartAnalysis('operator', 'operations', false)).toBe(true)
    expect(canStartAnalysis('operator', 'logistics', false)).toBe(false)
    expect(canUseKnowledge('supervisor', 'operations', false)).toBe(true)
    expect(canUseKnowledge('supervisor', 'logistics', false)).toBe(false)
    expect(canViewAgentObservability('supervisor', 'logistics', false)).toBe(false)
    expect(canViewAuditEvents('supervisor', 'logistics', false)).toBe(false)
    expect(canUseKnowledge('admin', 'logistics', false)).toBe(true)
  })

  it.each(['operator', 'supervisor', 'admin'] as UserRole[])(
    'enforces desktop and mobile actions for %s',
    (role) => {
      for (const mobile of [false, true]) {
        expect(canStartAnalysis(role, 'operations', mobile)).toBe(role === 'operator' && !mobile)
        expect(canSelectProduct(role, 'operations', mobile)).toBe(role === 'operator' && !mobile)
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
