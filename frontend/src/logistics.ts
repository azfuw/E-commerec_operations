import { apiRequest } from './api'

export type ShipmentStatus = 'pending_dispatch' | 'in_transit' | 'delivered'
export type ReturnStatus = 'requested' | 'approved' | 'in_transit' | 'received' | 'closed' | 'rejected'
export type ExceptionStatus = 'open' | 'in_progress' | 'resolved'
export type ExceptionKind = 'dispatch_overdue' | 'no_movement' | 'delivery_overdue' | 'return_overdue'

export interface LogisticsEvent {
  id: string
  shipment_id: string
  return_id: string | null
  event_type: string
  occurred_at: string
  location: string
  description: string
  actor_name: string
  source: 'manual' | 'demo' | 'agent'
  created_at: string
}

export interface Shipment {
  id: string
  order_id: string
  order_number: string
  store_id: string
  store_name: string
  carrier: string
  tracking_no: string
  status: ShipmentStatus
  destination: string
  dispatch_due_at: string
  expected_delivery_at: string
  dispatched_at: string | null
  delivered_at: string | null
  last_event_at: string | null
  last_location: string | null
  risk_level: 'normal' | 'medium' | 'high'
  issue_codes: string[]
  open_exception_count: number
  created_at: string
  is_demo: boolean
}

export interface ReturnCase {
  id: string
  shipment_id: string
  order_id: string
  order_number: string
  store_id: string
  store_name: string
  status: ReturnStatus
  reason: string
  carrier: string | null
  tracking_no: string | null
  requested_at: string
  expected_return_at: string | null
  received_at: string | null
  closed_at: string | null
  updated_at: string
  is_demo: boolean
  shipment_tracking_no: string
  destination: string
}

export interface LogisticsException {
  id: string
  shipment_id: string
  return_id: string | null
  order_number: string
  store_id: string
  store_name: string
  kind: ExceptionKind
  severity: 'medium' | 'high'
  status: ExceptionStatus
  title: string
  description: string
  recommended_action: string
  evidence: Record<string, unknown>
  assignee_id: string | null
  assignee_name: string | null
  created_at: string
  resolved_at: string | null
  resolution: string | null
}

export interface ShipmentDetail extends Shipment {
  events: LogisticsEvent[]
  exceptions: LogisticsException[]
  return_case: ReturnCase | null
}

export interface LogisticsDashboard {
  as_of: string
  metrics: {
    total_shipments: number
    pending_dispatch: number
    in_transit: number
    delivered_today: number
    open_exceptions: number
    active_returns: number
    on_time_rate: number | null
  }
  status_distribution: { status: ShipmentStatus; count: number }[]
  carriers: { carrier: string; total: number; delivered: number; on_time_rate: number | null }[]
  recent_exceptions: LogisticsException[]
  recent_events: LogisticsEvent[]
  agent: {
    mode: 'deterministic_rules'
    label: '规则巡检 Agent'
    carrier_connected: false
    last_run_at: string | null
    last_run_status: string | null
  }
  data_mode: 'demo' | 'operational' | 'mixed' | 'empty'
}

export interface LogisticsOrder {
  id: string
  store_id: string
  store_name: string
  order_number?: string
  ordered_at: string
  status: string
}

export interface AgentRun {
  id: string
  status: 'completed'
  mode: 'deterministic_rules'
  started_at: string
  completed_at: string
  scanned_shipments: number
  created_exceptions: number
  existing_exceptions: number
  summary: string
}

export interface AgentAnswer {
  answer: string
  mode: 'deterministic_rules'
  as_of: string
  citations: {
    shipment_id: string
    order_number: string
    tracking_no: string
    store_name: string
    status: string
    detail: string
  }[]
  suggestions: string[]
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export const shipmentStatusLabel: Record<ShipmentStatus, string> = {
  pending_dispatch: '\u5f85\u53d1\u8d27',
  in_transit: '\u8fd0\u8f93\u4e2d',
  delivered: '\u5df2\u7b7e\u6536',
}

export const returnStatusLabel: Record<ReturnStatus, string> = {
  requested: '\u5f85\u5ba1\u6838',
  approved: '\u5f85\u5bc4\u56de',
  in_transit: '\u9000\u56de\u4e2d',
  received: '\u5df2\u6536\u8d27',
  closed: '\u5df2\u5b8c\u6210',
  rejected: '\u5df2\u62d2\u7edd',
}

export const exceptionStatusLabel: Record<ExceptionStatus, string> = {
  open: '\u5f85\u5904\u7406',
  in_progress: '\u5904\u7406\u4e2d',
  resolved: '\u5df2\u89e3\u51b3',
}

export const exceptionKindLabel: Record<ExceptionKind, string> = {
  dispatch_overdue: '\u903e\u671f\u672a\u53d1',
  no_movement: '\u957f\u65f6\u95f4\u65e0\u8f68\u8ff9',
  delivery_overdue: '\u914d\u9001\u8d85\u65f6',
  return_overdue: '\u9000\u8d27\u6ede\u7559',
}

function query(values: Record<string, string | number | boolean | undefined>): string {
  return new URLSearchParams(
    Object.entries(values)
      .filter(([, value]) => value !== undefined && value !== '')
      .map(([key, value]) => [key, String(value)]),
  ).toString()
}

function json(method: string, body: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

export function formatShanghai(value: string | null | undefined): string {
  if (!value) return '\u2014'
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(value))
  const get = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? ''
  return `${get('year')}/${get('month')}/${get('day')} ${get('hour')}:${get('minute')}`
}

export function shanghaiInputValue(now = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(now.getTime() + 1000))
  const get = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}:${get('second')}`
}

export function toUtcIso(value: string): string {
  return new Date(`${value.length === 16 ? `${value}:00` : value}+08:00`).toISOString()
}

export function overviewEventDescription(value: string): string {
  return value
    .replace(/\s+任务 [0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}。$/i, '')
    .replace(/^异常任务 [0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}(?=（)/i, '异常工单')
}

function csvCell(value: unknown): string {
  let text = value == null ? '' : String(value)
  if (/^(?:[\t\r\n]|\s*[=+\-@])/.test(text)) text = `'${text}`
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
}

export function toCsv(headers: string[], rows: unknown[][]): string {
  return `\ufeff${[headers, ...rows].map((row) => row.map(csvCell).join(',')).join('\r\n')}`
}

export const getLogisticsDashboard = (storeId?: string) =>
  apiRequest<LogisticsDashboard>(`/logistics/dashboard?${query({ store_id: storeId })}`)

export const listShipments = (values: { store_id?: string; status?: string; q?: string; risk_only?: boolean; page: number; page_size: number }) =>
  apiRequest<Page<Shipment>>(`/logistics/shipments?${query(values)}`)

export const getShipment = (id: string) =>
  apiRequest<ShipmentDetail>(`/logistics/shipments/${encodeURIComponent(id)}`)

export const listEligibleOrders = (values: { q?: string; store_id?: string }) =>
  apiRequest<LogisticsOrder[]>(`/logistics/orders?${query(values)}`)

export const createShipment = (body: { order_id: string; carrier: string; tracking_no: string; destination: string; dispatch_due_at: string; expected_delivery_at: string }) =>
  apiRequest<ShipmentDetail>('/logistics/shipments', json('POST', body))

export const addShipmentEvent = (id: string, body: { action: 'dispatch' | 'transit' | 'deliver'; occurred_at: string; location: string; description: string }) =>
  apiRequest<ShipmentDetail>(`/logistics/shipments/${encodeURIComponent(id)}/events`, json('POST', body))

export const listReturns = (values: { store_id?: string; status?: string; q?: string; page: number; page_size: number }) =>
  apiRequest<Page<ReturnCase>>(`/logistics/returns?${query(values)}`)

export const createReturn = (body: { shipment_id: string; reason: string; expected_return_at: string }) =>
  apiRequest<ReturnCase>('/logistics/returns', json('POST', body))

export const transitionReturn = (id: string, body: { status: Exclude<ReturnStatus, 'requested'>; occurred_at: string; carrier?: string; tracking_no?: string; note: string }) =>
  apiRequest<ReturnCase>(`/logistics/returns/${encodeURIComponent(id)}/transition`, json('POST', body))

export const listExceptions = (values: { store_id?: string; status?: string; kind?: string; page: number; page_size: number }) =>
  apiRequest<Page<LogisticsException>>(`/logistics/exceptions?${query(values)}`)

export const listAssignees = (storeId?: string) =>
  apiRequest<{ id: string; username: string }[]>(`/logistics/assignees?${query({ store_id: storeId })}`)

export const updateException = (id: string, body: { action: 'assign' | 'resolve'; assignee_id?: string; resolution?: string }) =>
  apiRequest<LogisticsException>(`/logistics/exceptions/${encodeURIComponent(id)}`, json('PATCH', body))

export const runAgentPatrol = (storeId?: string) =>
  apiRequest<AgentRun>('/logistics/agent/patrol', json('POST', storeId ? { store_id: storeId } : {}))

export const listAgentRuns = (storeId?: string) =>
  apiRequest<AgentRun[]>(`/logistics/agent/runs?${query({ store_id: storeId })}`)

export const queryAgent = (question: string, storeId?: string) =>
  apiRequest<AgentAnswer>('/logistics/agent/query', json('POST', { question, ...(storeId ? { store_id: storeId } : {}) }))

export const getAgentBrief = (storeId?: string) =>
  apiRequest<AgentAnswer>(`/logistics/agent/brief?${query({ store_id: storeId })}`)
