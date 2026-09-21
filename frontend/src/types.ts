export type UserRole = 'operator' | 'supervisor' | 'admin'
export type UserDepartment = 'operations' | 'logistics'

export type UserStatus = 'active' | 'disabled'
export interface AdminUser {
  id: string; username: string; role: UserRole; department: UserDepartment; status: UserStatus
  store_ids: string[]; created_at: string
}
export interface AdminStore {
  id: string; name: string; code: string; enabled: boolean; created_at: string
}
export interface AdminUserQuery {
  page: number; page_size: number; role?: UserRole; status?: UserStatus; store_id?: string
}
export interface AdminStoreQuery { page: number; page_size: number; enabled?: boolean }
export interface AdminUserList { items: AdminUser[]; total: number; page: number; page_size: number }
export interface AdminStoreList { items: AdminStore[]; total: number; page: number; page_size: number }

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
  department: UserDepartment
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

export type EvidenceRef = {
  kind: 'fact' | 'citation'
  value: string
}

export type OutputCitation = {
  chunk_id: string
}

export type DescriptionSection = {
  heading: string
  body: string
  evidence: EvidenceRef[]
}

export type OptimizationChange = {
  field: 'title' | 'selling_points' | 'description' | 'keywords'
  current_value: string | string[] | DescriptionSection[]
  suggested_value: string | string[] | DescriptionSection[]
  reason: string
  evidence: EvidenceRef[]
}

export type AttributeCompletion = {
  target_attribute: string
  current_value: string | null
  suggested_value: string
  reason: string
  evidence: EvidenceRef[]
}

export type PriceSuggestion = {
  target_sku_id: string
  current_price: string
  suggested_price: string
  reason: string
  evidence: EvidenceRef[]
}

export type SkuSuggestion = {
  target_sku_id: string
  current_code: string
  current_spec: Record<string, string>
  suggested_code: string
  suggested_spec: Record<string, string>
  reason: string
  evidence: EvidenceRef[]
}

export type OptimizationProposalOutput = {
  title: string
  selling_points: string[]
  description: DescriptionSection[]
  keywords: string[]
  attribute_completions: AttributeCompletion[]
  changes: OptimizationChange[]
  citations: OutputCitation[]
  price_suggestions: PriceSuggestion[]
  sku_suggestions: SkuSuggestion[]
}

export type CanonicalRuleCitation = {
  document_id: string
  version_id: string
  chunk_id: string
  document_name: string
  version_number: number
  category: string
  canonical_text: string
  active: boolean
  applicable: boolean
}

export type ProposalRevision = {
  id: string
  iteration: number | null
  revision_number: number
  origin: 'agent' | 'manual'
  created_by: string
  parent_revision_id: string | null
  base_product_version: number
  proposal_output: OptimizationProposalOutput
  citations: CanonicalRuleCitation[]
}

export type RequiredChange = {
  source_track: string
  source_violation_code: string
  field: string
  instruction: string
  citation_chunk_ids: string[]
}

export type ComplianceReview = {
  id: string
  iteration: number | null
  deterministic_checks: Record<string, unknown>
  semantic_review: Record<string, unknown>
  passed: boolean
  risk_level: 'low' | 'medium' | 'high'
  quality_status: WorkflowQuality
  required_changes: RequiredChange[]
  citations: OutputCitation[]
  error_code: string | null
}

export type ProposalDetail = {
  proposal: {
    id: string
    analysis_run_id: string
    analysis_candidate_id: string
    optimization_run_id: string
    store_id: string
    product_id: string
    base_product_version: number
    current_revision_id: string | null
    created_at: string
    updated_at: string
  }
  optimization_run: {
    id: string
    workflow_type: WorkflowType
    status: WorkflowStatus
    quality_status: WorkflowQuality
    error_code: string | null
  }
  current_revision: ProposalRevision | null
  current_review: ComplianceReview | null
  active_manual_review: {
    manual_review_run_id: string
    workflow_run_id: string
    proposal_revision_id: string
    status: WorkflowStatus
    quality_status: WorkflowQuality
    current_step: string | null
    error_code: string | null
  } | null
  submitted_revision: ProposalRevision | null
  latest_action: ApprovalAction | null
  publish_record: PublishRecord | null
}

export type ManualRevisionRequest = {
  parent_revision_id: string
  base_product_version: number
  title: string
  selling_points: string[]
  description: DescriptionSection[]
  keywords: string[]
  attribute_completions: AttributeCompletion[]
  changes: OptimizationChange[]
}

export type ManualRevisionAccepted = {
  revision_id: string
  manual_review_workflow_run_id: string
  status: 'accepted'
}

export type ApprovalAction = {
  id: string
  proposal_id: string
  proposal_revision_id: string
  actor_id: string
  actor_role: UserRole
  action: 'submit' | 'approve' | 'reject' | 'request_changes'
  comment: string | null
  created_at: string
}

export type PlatformDelivery = {
  status: 'pending' | 'processing' | 'succeeded' | 'failed'
  attempt_count: number
  external_operation_id: string | null
  error_code: string | null
  completed_at: string | null
}

export type PublishRecord = {
  id: string
  proposal_id: string
  proposal_revision_id: string
  product_id: string
  store_id: string
  approved_by: string
  approval_action_id: string
  before_snapshot: Record<string, unknown>
  after_snapshot: Record<string, unknown>
  base_product_version: number
  published_product_version: number
  published_at: string
  platform_delivery: PlatformDelivery | null
}

export type ApprovalListItem = {
  proposal_id: string
  proposal_revision_id: string
  revision_number: number
  store_id: string
  product_id: string
  submitted_by: string
  status: 'pending_approval'
  submitted_at: string
}

export type ApprovalList = {
  items: ApprovalListItem[]
  page: number
  page_size: number
  total: number
}


export interface KnowledgeSearchQuery {
  store_id: string
  query: string
  categories?: string[]
  top_k?: number
}
export interface KnowledgeCitation {
  chunk_id: string
  document_name: string
  version_number: number
  category: string
  canonical_text: string
  final_score: number
}
export interface KnowledgeEnvelope<T> {
  request_id: string
  status: 'accepted' | 'success' | 'error'
  data: T
  quality: { status: 'ok' | 'zero_hit' | 'low_confidence' } | null
  error: { code: string; category: string } | null
}
export type KnowledgeSearchResult = KnowledgeEnvelope<{ citations: KnowledgeCitation[] }>

export interface KnowledgeDocument {
  document_id: string
  name: string
  category: string
  enabled: boolean
  current_version_id: string | null
  current_version_status: string | null
}
export interface KnowledgeDocumentList {
  items: KnowledgeDocument[]
  page: number
  page_size: number
  total: number
}
export interface KnowledgeVersion {
  id: string
  version_number: number
  status: string
  parser_version: string | null
  chunker_version: string | null
  embedding_version: string | null
  error_code: string | null
  created_at: string
}
export interface KnowledgeVersionHistory {
  document_id: string
  name: string
  category: string
  enabled: boolean
  items: KnowledgeVersion[]
  page: number
  page_size: number
  total: number
}

export type EvaluationAgentType = 'analysis' | 'optimization' | 'compliance' | 'knowledge_retrieval'
export interface EvaluationRun {
  id: string
  agent_type: EvaluationAgentType
  store_id: string | null
  suite_version: string
  runner_version: string
  dataset_version: string
  execution_mode: 'offline_fixture'
  status: 'completed' | 'failed'
  started_at: string
  completed_at: string
  created_at: string
  summary: Record<string, number>
  error_code: string | null
}
export interface EvaluationResult {
  case_key: string
  case_version: number
  agent_type: EvaluationAgentType
  outcome: 'passed' | 'failed'
  metrics: Record<string, boolean | number>
  result_code: string
  latency_ms: number
}
export interface EvaluationRunDetail extends EvaluationRun { results: EvaluationResult[] }
export interface EvaluationRunList { items: EvaluationRun[]; total: number; page: number; page_size: number }
export interface AgentCall {
  id: string; workflow_run_id: string; store_id: string; workflow_type: string
  node_name: string; call_type: string; iteration: number; attempt: number
  model: string; prompt_version: string; status: 'succeeded' | 'failed'
  prompt_tokens: number; completion_tokens: number; total_tokens: number
  duration_ms: number; estimated_cost: string | null; error_code: string | null; created_at: string
}
export interface AgentCallList { items: AgentCall[]; total: number; page: number; page_size: number }
export interface EvaluationQuery { page: number; page_size: number; agent_type?: EvaluationAgentType; status?: 'completed' | 'failed'; store_id?: string }
export interface AgentCallQuery { page: number; page_size: number; store_id?: string; workflow_type?: string; node_name?: string; status?: 'succeeded' | 'failed'; error_code?: string }

export interface AuditEvent {
  id: string; event_type: string; outcome: string; actor_id: string | null; actor_role: UserRole | null
  store_id: string | null; proposal_id: string | null; proposal_revision_id: string | null
  workflow_run_id: string | null; approval_action_id: string | null; publish_record_id: string | null
  resource_type: string | null; resource_id: string | null; request_id: string | null
  error_code: string | null; details: Record<string, string | boolean | number | string[]>; created_at: string
}
export interface AuditEventQuery {
  page: number; page_size: number; store_id?: string; proposal_id?: string; workflow_run_id?: string
  action?: string; event_type?: string; actor_id?: string; outcome?: string; created_from?: string; created_to?: string
}
export interface AuditEventList { items: AuditEvent[]; total: number; page: number; page_size: number }
