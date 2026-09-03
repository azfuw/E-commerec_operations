import { clearSession, session, setCurrentUser, setToken } from './session'
import type {
  AccessToken,
  AnalysisCandidate,
  AnalysisRunAccepted,
  AnalysisRunRequest,
  CurrentUser,
  ProductSelection,
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
