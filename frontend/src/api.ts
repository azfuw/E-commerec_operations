import { clearSession, session, setCurrentUser, setToken } from './session'
import type {
  AccessToken,
  AnalysisCandidate,
  AnalysisRunAccepted,
  AnalysisRunRequest,
  CurrentUser,
  ProductSelection,
  ProposalDetail,
  ManualRevisionAccepted,
  ManualRevisionRequest,
  ApprovalAction,
  ApprovalList,
  PublishRecord,
  StoreSummary,
  TaskKind,
  WorkbenchTaskList,
  WorkflowRun,
  WorkflowStatus,
} from './types'

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly requestId?: string,
    public readonly fieldErrors: Record<string, string> = {},
  ) {
    super(code)
  }
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null
    ? (value as Record<string, unknown>)
    : null
}

function apiError(status: number, payload: unknown): ApiError {
  const body = record(payload)
  const detail = body?.detail
  const detailObject = record(detail)
  const knowledgeError = record(body?.error)
  let code = `HTTP_${status}`
  const fieldErrors: Record<string, string> = {}

  if (typeof detail === 'string') code = detail
  if (typeof detailObject?.code === 'string') code = detailObject.code
  if (typeof knowledgeError?.code === 'string') code = knowledgeError.code
  if (Array.isArray(detail)) {
    code = 'VALIDATION_ERROR'
    for (const issue of detail) {
      const item = record(issue)
      const location = Array.isArray(item?.loc) ? item.loc.at(-1) : undefined
      if (
        (typeof location === 'string' || typeof location === 'number') &&
        typeof item?.msg === 'string'
      ) {
        fieldErrors[String(location)] = item.msg
      }
    }
  }

  const requestId =
    typeof body?.request_id === 'string'
      ? body.request_id
      : typeof detailObject?.request_id === 'string'
        ? detailObject.request_id
        : undefined
  return new ApiError(status, code, requestId, fieldErrors)
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers)
  if (session.token) headers.set('Authorization', `Bearer ${session.token}`)
  const request: RequestInit = { ...init, headers }
  if ((init.method ?? 'GET').toUpperCase() === 'GET') request.cache = 'no-store'

  const response = await fetch(path, request)
  if (response.status === 204) return undefined as T
  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const error = apiError(response.status, payload)
    if (response.status === 401) clearSession()
    throw error
  }
  return payload as T
}

export async function login(username: string, password: string): Promise<void> {
  clearSession()
  const token = await apiRequest<AccessToken>('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  setToken(token.access_token)
  try {
    setCurrentUser(await apiRequest<CurrentUser>('/auth/me'))
  } catch (error) {
    clearSession()
    throw error
  }
}

export async function restoreSession(): Promise<void> {
  if (!session.token) {
    session.ready = true
    return
  }
  try {
    setCurrentUser(await apiRequest<CurrentUser>('/auth/me'))
  } catch {
    session.user = null
  } finally {
    session.ready = true
  }
}

export async function listWorkbenchTasks(
  query: {
    page: number
    pageSize: number
    storeId?: string
    kind?: TaskKind
    status?: WorkflowStatus
  },
  signal?: AbortSignal,
): Promise<WorkbenchTaskList> {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
  })
  if (query.storeId) params.set('store_id', query.storeId)
  if (query.kind) params.set('kind', query.kind)
  if (query.status) params.set('status', query.status)
  const request: RequestInit = {}
  if (signal) request.signal = signal
  return apiRequest<WorkbenchTaskList>(`/workbench/tasks?${params}`, request)
}

export async function listStores(signal?: AbortSignal): Promise<StoreSummary[]> {
  const request: RequestInit = {}
  if (signal) request.signal = signal
  return apiRequest<StoreSummary[]>('/stores', request)
}

export function createAnalysisRun(body: AnalysisRunRequest): Promise<AnalysisRunAccepted> {
  return apiRequest<AnalysisRunAccepted>('/analysis-runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function getWorkflowRun(id: string, signal?: AbortSignal): Promise<WorkflowRun> {
  const request: RequestInit = {}
  if (signal) request.signal = signal
  return apiRequest<WorkflowRun>(`/workflow-runs/${encodeURIComponent(id)}`, request)
}

export function listAnalysisCandidates(
  id: string,
  signal?: AbortSignal,
): Promise<AnalysisCandidate[]> {
  const request: RequestInit = {}
  if (signal) request.signal = signal
  return apiRequest<AnalysisCandidate[]>(
    `/analysis-runs/${encodeURIComponent(id)}/candidates`,
    request,
  )
}

export function selectProduct(
  analysisRunId: string,
  candidateId: string,
  idempotencyKey: string,
): Promise<ProductSelection> {
  return apiRequest<ProductSelection>(
    `/analysis-runs/${encodeURIComponent(analysisRunId)}/select-product`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
      },
      body: JSON.stringify({ candidate_id: candidateId }),
    },
  )
}

export function getProposal(id: string, signal?: AbortSignal): Promise<ProposalDetail> {
  const request: RequestInit = {}
  if (signal) request.signal = signal
  return apiRequest<ProposalDetail>(`/proposals/${encodeURIComponent(id)}`, request)
}

export function createManualRevision(
  proposalId: string,
  body: ManualRevisionRequest,
  idempotencyKey: string,
): Promise<ManualRevisionAccepted> {
  return apiRequest<ManualRevisionAccepted>(
    `/proposals/${encodeURIComponent(proposalId)}/manual-revision`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
      },
      body: JSON.stringify(body),
    },
  )
}

export function submitProposal(
  proposalId: string,
  revisionId: string,
  idempotencyKey: string,
): Promise<ApprovalAction> {
  return apiRequest<ApprovalAction>(`/proposals/${encodeURIComponent(proposalId)}/submit`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
    },
    body: JSON.stringify({ revision_id: revisionId }),
  })
}

export function listApprovals(
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<ApprovalList> {
  const request: RequestInit = {}
  if (signal) request.signal = signal
  return apiRequest<ApprovalList>(`/approvals?page=${page}&page_size=${pageSize}`, request)
}

export function approveProposal(
  id: string,
  revisionId: string,
  key: string,
): Promise<PublishRecord> {
  return apiRequest<PublishRecord>(`/approvals/${encodeURIComponent(id)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify({ revision_id: revisionId }),
  })
}

export function rejectProposal(
  id: string,
  revisionId: string,
  comment: string,
  key: string,
): Promise<ApprovalAction> {
  return apiRequest<ApprovalAction>(`/approvals/${encodeURIComponent(id)}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify({ revision_id: revisionId, comment }),
  })
}

export function requestProposalChanges(
  id: string,
  revisionId: string,
  comment: string,
  key: string,
): Promise<ApprovalAction> {
  return apiRequest<ApprovalAction>(`/approvals/${encodeURIComponent(id)}/request-changes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify({ revision_id: revisionId, comment }),
  })
}


export function searchKnowledge(query: import('./types').KnowledgeSearchQuery, signal?: AbortSignal): Promise<import('./types').KnowledgeSearchResult> {
  return apiRequest('/knowledge/search', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(query), ...(signal ? { signal } : {}),
  })
}

export function listKnowledgeDocuments(query: { page: number; category?: string; enabled?: boolean; version_status?: string }, signal?: AbortSignal): Promise<import('./types').KnowledgeEnvelope<import('./types').KnowledgeDocumentList>> {
  const params = new URLSearchParams({ page: String(query.page), page_size: '20' })
  if (query.category) params.set('category', query.category)
  if (query.enabled !== undefined) params.set('enabled', String(query.enabled))
  if (query.version_status) params.set('version_status', query.version_status)
  return apiRequest(`/knowledge/documents?${params}`, signal ? { signal } : {})
}
export function getKnowledgeVersions(id: string, page: number, signal?: AbortSignal): Promise<import('./types').KnowledgeVersionHistory> {
  return apiRequest(`/knowledge/documents/${encodeURIComponent(id)}/versions?page=${page}&page_size=20`, signal ? { signal } : {})
}
export function uploadKnowledgeDocument(body: FormData, documentId?: string, signal?: AbortSignal): Promise<import('./types').KnowledgeEnvelope<{ document_id: string; version_id: string; status: string }>> {
  return apiRequest(documentId ? `/knowledge/documents/${encodeURIComponent(documentId)}/versions` : '/knowledge/documents', {
    method: 'POST', body, headers: { 'Idempotency-Key': crypto.randomUUID() }, ...(signal ? { signal } : {}),
  })
}
export function disableKnowledgeDocument(id: string, signal?: AbortSignal): Promise<import('./types').KnowledgeEnvelope<{ document_id: string; enabled: boolean }>> {
  return apiRequest(`/knowledge/documents/${encodeURIComponent(id)}/disable`, { method: 'POST', ...(signal ? { signal } : {}) })
}

export function listAgentEvaluationRuns(query: import('./types').EvaluationQuery, signal?: AbortSignal): Promise<import('./types').EvaluationRunList> {
  const params = new URLSearchParams(Object.entries(query).filter(([,v]) => v !== undefined && v !== '').map(([k,v]) => [k,String(v)]))
  return apiRequest(`/agent-evaluations/runs?${params}`, signal ? {signal} : {})
}
export function getAgentEvaluationRun(id: string, signal?: AbortSignal): Promise<import('./types').EvaluationRunDetail> {
  return apiRequest(`/agent-evaluations/runs/${encodeURIComponent(id)}`, signal ? {signal} : {})
}
export function listAgentCalls(query: import('./types').AgentCallQuery, signal?: AbortSignal): Promise<import('./types').AgentCallList> {
  const params = new URLSearchParams(Object.entries(query).filter(([,v]) => v !== undefined && v !== '').map(([k,v]) => [k,String(v)]))
  return apiRequest(`/agent-calls?${params}`, signal ? {signal} : {})
}
