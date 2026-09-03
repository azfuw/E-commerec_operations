export type UserRole = 'operator' | 'supervisor' | 'admin'

export type WorkflowStatus =
  | 'accepted'
  | 'processing'
  | 'awaiting_selection'
  | 'completed'
  | 'draft_ready'
  | 'pending_manual'
  | 'pending_approval'
  | 'rejected'
  | 'failed'

export type CurrentUser = {
  id: string
  username: string
  role: UserRole
}

export type AccessToken = {
  access_token: string
  token_type: 'bearer'
}
