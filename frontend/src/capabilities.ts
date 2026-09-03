import type { UserRole, WorkflowStatus } from './types'

export function canStartAnalysis(role: UserRole, isMobile: boolean): boolean {
  return role === 'operator' && !isMobile
}

export function canSelectProduct(role: UserRole, isMobile: boolean): boolean {
  return role === 'operator' && !isMobile
}

export function canEditProposal(
  _role: UserRole,
  status: WorkflowStatus,
  isMobile: boolean,
): boolean {
  return !isMobile && (status === 'draft_ready' || status === 'pending_manual')
}

export function canSubmitProposal(
  _role: UserRole,
  status: WorkflowStatus,
  isMobile: boolean,
): boolean {
  return !isMobile && status === 'draft_ready'
}

export function canApprove(role: UserRole, status: WorkflowStatus): boolean {
  return (role === 'supervisor' || role === 'admin') && status === 'pending_approval'
}
