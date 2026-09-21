import type { CurrentUser, UserDepartment, UserRole, WorkflowStatus } from './types'

export function canAccessDepartment(
  role: UserRole,
  department: UserDepartment,
  target: UserDepartment,
): boolean {
  return role === 'admin' || department === target
}

export function homeLocation(
  user: CurrentUser,
  preferred?: unknown,
): { name: 'logistics' | 'workbench' } {
  const department = user.role === 'admin' && (preferred === 'operations' || preferred === 'logistics')
    ? preferred
    : user.department
  return { name: department === 'logistics' ? 'logistics' : 'workbench' }
}

export function canManageSystem(role: UserRole, isMobile: boolean): boolean {
  return !isMobile && role === 'admin'
}

export function canUseKnowledge(role: UserRole, department: UserDepartment, isMobile: boolean): boolean {
  return !isMobile && canAccessDepartment(role, department, 'operations')
}

export function canViewAgentObservability(role: UserRole, department: UserDepartment, isMobile: boolean): boolean {
  return !isMobile && canAccessDepartment(role, department, 'operations') && (role === 'supervisor' || role === 'admin')
}

export function canViewAuditEvents(role: UserRole, department: UserDepartment, isMobile: boolean): boolean {
  return !isMobile && canAccessDepartment(role, department, 'operations') && (role === 'supervisor' || role === 'admin')
}

export function canStartAnalysis(role: UserRole, department: UserDepartment, isMobile: boolean): boolean {
  return role === 'operator' && department === 'operations' && !isMobile
}

export function canSelectProduct(role: UserRole, department: UserDepartment, isMobile: boolean): boolean {
  return role === 'operator' && department === 'operations' && !isMobile
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

export function canApprove(
  role: UserRole,
  status: WorkflowStatus,
  _isMobile = false,
): boolean {
  return (role === 'supervisor' || role === 'admin') && status === 'pending_approval'
}
