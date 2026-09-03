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

export type WorkflowQuality = 'normal' | 'partial' | 'degraded'

export type WorkflowType = 'analysis' | 'optimization' | 'manual_review'

export type TaskKind = 'analysis' | 'proposal'

export type WorkbenchAction =
  | 'wait'
  | 'select_product'
  | 'edit_proposal'
  | 'submit_proposal'
  | 'review_approval'
  | 'view_result'
  | 'resolve_failure'

export type CurrentUser = {
  id: string
  username: string
  role: UserRole
}

export type AccessToken = {
  access_token: string
  token_type: 'bearer'
}

export type StoreSummary = {
  id: string
  code: string
  name: string
}

export type WorkbenchTask = {
  id: string
  kind: TaskKind
  store_id: string
  product_id: string | null
  analysis_run_id: string
  proposal_id: string | null
  workflow_run_id: string
  workflow_type: WorkflowType
  status: WorkflowStatus
  quality_status: WorkflowQuality
  current_step: string | null
  action_required: WorkbenchAction
  requires_current_user_action: boolean
  created_by: string
  updated_at: string
}

export type WorkbenchTaskList = {
  items: WorkbenchTask[]
  page: number
  page_size: number
  total: number
}

export type AnalysisRunRequest = {
  store_id: string
  start_date: string
  end_date: string
}

export type AnalysisRunAccepted = {
  workflow_run_id: string
  status: 'accepted'
}

export type WorkflowRun = {
  id: string
  workflow_type: WorkflowType
  store_id: string
  start_date: string | null
  end_date: string | null
  status: WorkflowStatus
  quality_status: WorkflowQuality
  current_step: string | null
  attempt_count: number
  candidates_ready: boolean
  error_code: string | null
}

export type ProductMetrics = {
  product_id: string | null
  product_code: string | null
  impressions: number
  clicks: number
  orders: number
  units: number
  revenue: string
  refunds: number
  ctr: string
  conversion_rate: string
  refund_rate: string
  average_order_value: string
}

export type AnalysisCandidate = {
  id: string
  product_id: string
  rank: number
  product_code: string
  anomaly_types: string[]
  metrics: ProductMetrics
  business_impact: string
  evidence: string[]
  impact_explanation: string
  reason: string
  recommended_action: string
  confidence: string
}

export type ProductSelection = {
  proposal_id: string
  optimization_workflow_run_id: string
  status: 'accepted'
}
