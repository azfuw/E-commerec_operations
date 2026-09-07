import type { Page, Request, Route } from '@playwright/test'

import type {
  ApprovalAction,
  ComplianceReview,
  ProposalRevision,
  UserRole,
  WorkflowStatus,
} from '../src/types'

type FixtureUser = { id: string; username: string; role: UserRole; token: string }

type ManualRevisionBody = {
  parent_revision_id?: unknown
  base_product_version?: unknown
  title?: unknown
}

type FixtureState = {
  analysisCreated: boolean
  analysisPolls: number
  proposalExists: boolean
  proposalStatus: WorkflowStatus
  currentRevisionId: string
  submittedRevisionId: string | null
  firstManualRevisionId: string | null
  activeManualWorkflowId: string | null
  activeManualRevisionId: string | null
  nextRevisionNumber: number
  revisions: Map<string, ProposalRevision>
  latestAction: ApprovalAction | null
  publishRecord: Record<string, unknown> | null
  protectedRequests: number
  checkedWritePaths: Set<string>
}

const users: Record<'operator' | 'supervisor' | 'admin', FixtureUser> = {
  operator: { id: 'operator-1', username: 'operator', role: 'operator', token: 'operator-token' },
  supervisor: { id: 'supervisor-1', username: 'supervisor', role: 'supervisor', token: 'supervisor-token' },
  admin: { id: 'admin-1', username: 'admin', role: 'admin', token: 'admin-token' },
}

const store = { id: 'store-1', code: 'MAIN', name: '旗舰店' }
const candidate = {
  id: 'candidate-1',
  product_id: 'product-1',
  rank: 1,
  product_code: 'PRODUCT-1',
  anomaly_types: ['转化率下降'],
  metrics: {
    product_id: 'product-1',
    product_code: 'PRODUCT-1',
    impressions: 1000,
    clicks: 125,
    orders: 15,
    units: 15,
    revenue: '321.00',
    refunds: 1,
    ctr: '0.1250',
    conversion_rate: '0.1200',
    refund_rate: '0.0667',
    average_order_value: '21.40',
  },
  business_impact: '需要改善转化',
  evidence: ['近七日转化率下降'],
  impact_explanation: '转化率下降影响订单',
  reason: '优先优化商品详情',
  recommended_action: '进入优化流程',
  confidence: '0.93',
}

const idempotentWrites = new Set([
  '/analysis-runs/analysis-1/select-product',
  '/proposals/proposal-1/manual-revision',
  '/proposals/proposal-1/submit',
  '/approvals/proposal-1/approve',
  '/approvals/proposal-1/reject',
  '/approvals/proposal-1/request-changes',
])

function revision(id: string, revisionNumber: number, title: string, parentRevisionId: string | null): ProposalRevision {
  return {
    id,
    iteration: parentRevisionId ? null : 0,
    revision_number: revisionNumber,
    origin: parentRevisionId ? 'manual' : 'agent',
    created_by: parentRevisionId ? 'operator-1' : 'agent',
    parent_revision_id: parentRevisionId,
    base_product_version: 7,
    proposal_output: {
      title,
      selling_points: ['舒适透气'],
      description: [
        {
          heading: '商品详情',
          body: '可信商品详情',
          evidence: [{ kind: 'citation', value: 'chunk-1' }],
        },
      ],
      keywords: ['纯棉', '短袖'],
      attribute_completions: [
        {
          target_attribute: '材质',
          current_value: '棉',
          suggested_value: '精梳棉',
          reason: '补全材质',
          evidence: [{ kind: 'fact', value: 'product.attributes.材质' }],
        },
      ],
      changes: [
        {
          field: 'title',
          current_value: '原商品标题',
          suggested_value: title,
          reason: '优化标题表达',
          evidence: [{ kind: 'fact', value: 'product.title' }],
        },
        {
          field: 'selling_points',
          current_value: ['原卖点'],
          suggested_value: ['舒适透气'],
          reason: '优化卖点表达',
          evidence: [{ kind: 'fact', value: 'product.selling_points' }],
        },
        {
          field: 'description',
          current_value: '原始详情',
          suggested_value: [
            {
              heading: '商品详情',
              body: '可信商品详情',
              evidence: [{ kind: 'citation', value: 'chunk-1' }],
            },
          ],
          reason: '优化详情表达',
          evidence: [{ kind: 'citation', value: 'chunk-1' }],
        },
        {
          field: 'keywords',
          current_value: ['原关键词'],
          suggested_value: ['纯棉', '短袖'],
          reason: '优化搜索词',
          evidence: [{ kind: 'fact', value: 'product.search_keywords' }],
        },
      ],
      citations: [{ chunk_id: 'chunk-1' }],
      price_suggestions: [
        {
          target_sku_id: 'sku-1',
          current_price: '100.00',
          suggested_price: '90.00',
          reason: '价格建议',
          evidence: [{ kind: 'citation', value: 'chunk-1' }],
        },
      ],
      sku_suggestions: [
        {
          target_sku_id: 'sku-1',
          current_code: 'SKU-RED',
          current_spec: { 颜色: '红' },
          suggested_code: 'SKU-RED-NEW',
          suggested_spec: { 颜色: '红色' },
          reason: 'SKU 建议',
          evidence: [{ kind: 'citation', value: 'chunk-1' }],
        },
      ],
    },
    citations: [
      {
        document_id: 'document-1',
        version_id: 'version-1',
        chunk_id: 'chunk-1',
        document_name: '通用规则',
        version_number: 1,
        category: '通用规则',
        canonical_text: '仅展示可信事实',
        active: true,
        applicable: true,
      },
    ],
  }
}

function review(passed: boolean): ComplianceReview {
  return {
    id: passed ? 'review-passed' : 'review-pending',
    iteration: null,
    deterministic_checks: { passed },
    semantic_review: { passed },
    passed,
    risk_level: passed ? 'low' : 'medium',
    quality_status: 'normal',
    required_changes: passed
      ? []
      : [
          {
            source_track: 'semantic',
            source_violation_code: 'TITLE_REVIEW',
            field: 'title',
            instruction: '人工修订后重新复核',
            citation_chunk_ids: ['chunk-1'],
          },
        ],
    citations: [{ chunk_id: 'chunk-1' }],
    error_code: null,
  }
}

function actorFor(request: Request): FixtureUser | null {
  const authorization = request.headers()['authorization']
  return Object.values(users).find((user) => authorization === `Bearer ${user.token}`) ?? null
}

function writeBody(request: Request): Record<string, unknown> {
  try {
    const body = request.postDataJSON()
    return typeof body === 'object' && body !== null ? body as Record<string, unknown> : {}
  } catch {
    return {}
  }
}

function action(
  actor: FixtureUser,
  kind: ApprovalAction['action'],
  revisionId: string,
  comment: string | null,
): ApprovalAction {
  return {
    id: `action-${kind}-${revisionId}`,
    proposal_id: 'proposal-1',
    proposal_revision_id: revisionId,
    actor_id: actor.id,
    actor_role: actor.role,
    action: kind,
    comment,
    created_at: '2026-09-03T08:00:00Z',
  }
}

export type ApiFixtureController = {
  seedPendingApproval(): void
  firstManualRevision(): { id: string; title: string } | null
  assertProtocol(): void
}

export async function installApiFixture(page: Page): Promise<ApiFixtureController> {
  const first = revision('revision-1', 1, '初始可信标题', null)
  const state: FixtureState = {
    analysisCreated: false,
    analysisPolls: 0,
    proposalExists: false,
    proposalStatus: 'pending_manual',
    currentRevisionId: first.id,
    submittedRevisionId: null,
    firstManualRevisionId: null,
    activeManualWorkflowId: null,
    activeManualRevisionId: null,
    nextRevisionNumber: 2,
    revisions: new Map([[first.id, first]]),
    latestAction: null,
    publishRecord: null,
    protectedRequests: 0,
    checkedWritePaths: new Set(),
  }

  const currentRevision = (): ProposalRevision => state.revisions.get(state.currentRevisionId) ?? first
  const proposalDetail = () => {
    const active = state.activeManualWorkflowId
    return {
      proposal: {
        id: 'proposal-1',
        analysis_run_id: 'analysis-1',
        analysis_candidate_id: candidate.id,
        optimization_run_id: 'optimization-1',
        store_id: store.id,
        product_id: 'product-1',
        base_product_version: 7,
        current_revision_id: currentRevision().id,
        created_at: '2026-09-03T08:00:00Z',
        updated_at: '2026-09-03T08:00:00Z',
      },
      optimization_run: {
        id: 'optimization-1',
        workflow_type: 'optimization',
        status: state.proposalStatus,
        quality_status: 'normal',
        error_code: null,
      },
      current_revision: currentRevision(),
      current_review: review(state.proposalStatus === 'draft_ready' || state.proposalStatus === 'pending_approval' || state.proposalStatus === 'completed'),
      active_manual_review: active
        ? {
            manual_review_run_id: `manual-run-${active}`,
            workflow_run_id: active,
            proposal_revision_id: state.activeManualRevisionId,
            status: 'accepted',
            quality_status: 'normal',
            current_step: 'manual_review',
            error_code: null,
          }
        : null,
      submitted_revision: state.submittedRevisionId ? state.revisions.get(state.submittedRevisionId) ?? null : null,
      latest_action: state.latestAction,
      publish_record: state.publishRecord,
    }
  }

  const workbenchTasks = (actor: FixtureUser) => {
    if (state.proposalExists) {
      const approval = state.proposalStatus === 'pending_approval'
      return [{
        id: 'proposal-1',
        kind: 'proposal',
        store_id: store.id,
        product_id: 'product-1',
        analysis_run_id: 'analysis-1',
        proposal_id: 'proposal-1',
        workflow_run_id: state.activeManualWorkflowId ?? 'optimization-1',
        workflow_type: state.activeManualWorkflowId ? 'manual_review' : 'optimization',
        status: state.proposalStatus,
        quality_status: 'normal',
        current_step: approval ? 'approval_pending' : 'manual_review',
        action_required: approval && actor.role !== 'operator' ? 'review_approval' : state.proposalStatus === 'pending_manual' ? 'edit_proposal' : approval ? 'wait' : 'view_result',
        requires_current_user_action: approval ? actor.role !== 'operator' : state.proposalStatus === 'pending_manual',
        created_by: 'operator-1',
        updated_at: '2026-09-03T08:00:00Z',
      }]
    }
    if (state.analysisCreated) {
      return [{
        id: 'analysis-1',
        kind: 'analysis',
        store_id: store.id,
        product_id: null,
        analysis_run_id: 'analysis-1',
        proposal_id: null,
        workflow_run_id: 'analysis-1',
        workflow_type: 'analysis',
        status: 'awaiting_selection',
        quality_status: 'normal',
        current_step: 'select_product',
        action_required: actor.role === 'operator' ? 'select_product' : 'view_result',
        requires_current_user_action: actor.role === 'operator',
        created_by: 'operator-1',
        updated_at: '2026-09-03T08:00:00Z',
      }]
    }
    return []
  }

  async function respond(route: Route, json: unknown, status = 200): Promise<void> {
    await route.fulfill({ status, json })
  }

  async function reject(route: Route, status: number, code: string): Promise<void> {
    await respond(route, { detail: { code } }, status)
  }

  await page.route(/^http:\/\/127\.0\.0\.1:4173\/(?:auth|stores|analysis-runs|workflow-runs|workbench|proposals|approvals)(?:\/|\?|$)/u, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()

    if (path === '/auth/login' && method === 'POST') {
      const username = writeBody(request).username
      const user = typeof username === 'string' && username in users
        ? users[username as keyof typeof users]
        : null
      if (!user) {
        await reject(route, 401, 'LOGIN_FAILED')
        return
      }
      await respond(route, { access_token: user.token, token_type: 'bearer' })
      return
    }

    const actor = actorFor(request)
    if (!actor) {
      await reject(route, 401, 'AUTH_REQUIRED')
      return
    }
    state.protectedRequests += 1
    if (idempotentWrites.has(path) && method === 'POST') {
      if (!request.headers()['idempotency-key']) {
        await reject(route, 400, 'IDEMPOTENCY_KEY_REQUIRED')
        return
      }
      state.checkedWritePaths.add(path)
    }

    if (path === '/auth/me' && method === 'GET') {
      await respond(route, { id: actor.id, username: actor.username, role: actor.role })
      return
    }
    if (path === '/stores' && method === 'GET') {
      await respond(route, [store])
      return
    }
    if (path === '/workbench/tasks' && method === 'GET') {
      const all = workbenchTasks(actor)
      const items = url.searchParams.get('kind') === 'proposal' ? all.filter((item) => item.kind === 'proposal') : all
      await respond(route, { items, page: 1, page_size: 20, total: items.length })
      return
    }
    if (path === '/analysis-runs' && method === 'POST') {
      if (actor.role !== 'operator') {
        await reject(route, 403, 'ANALYSIS_FORBIDDEN')
        return
      }
      state.analysisCreated = true
      state.analysisPolls = 0
      await respond(route, { workflow_run_id: 'analysis-1', status: 'accepted' }, 202)
      return
    }
    if (path === '/workflow-runs/analysis-1' && method === 'GET') {
      if (!state.analysisCreated) {
        await reject(route, 404, 'WORKFLOW_NOT_FOUND')
        return
      }
      const awaitingSelection = state.analysisPolls > 0
      state.analysisPolls += 1
      await respond(route, {
        id: 'analysis-1',
        workflow_type: 'analysis',
        store_id: store.id,
        start_date: '2026-08-01',
        end_date: '2026-08-31',
        status: awaitingSelection ? 'awaiting_selection' : 'processing',
        quality_status: 'normal',
        current_step: awaitingSelection ? 'select_product' : 'ranking',
        attempt_count: 1,
        candidates_ready: awaitingSelection,
        error_code: null,
      })
      return
    }
    if (path === '/analysis-runs/analysis-1/candidates' && method === 'GET') {
      if (!state.analysisCreated) {
        await reject(route, 404, 'ANALYSIS_NOT_FOUND')
        return
      }
      await respond(route, [candidate])
      return
    }
    if (path === '/analysis-runs/analysis-1/select-product' && method === 'POST') {
      if (actor.role !== 'operator' || writeBody(request).candidate_id !== candidate.id) {
        await reject(route, 403, 'SELECTION_FORBIDDEN')
        return
      }
      state.proposalExists = true
      state.proposalStatus = 'pending_manual'
      await respond(route, {
        proposal_id: 'proposal-1',
        optimization_workflow_run_id: 'optimization-1',
        status: 'accepted',
      })
      return
    }
    if (path === '/proposals/proposal-1' && method === 'GET') {
      if (!state.proposalExists) {
        await reject(route, 404, 'PROPOSAL_NOT_FOUND')
        return
      }
      await respond(route, proposalDetail())
      return
    }
    if (path === '/proposals/proposal-1/manual-revision' && method === 'POST') {
      if (!['operator', 'supervisor', 'admin'].includes(actor.role) || state.proposalStatus !== 'pending_manual') {
        await reject(route, 409, 'MANUAL_REVISION_NOT_ALLOWED')
        return
      }
      const body = writeBody(request) as ManualRevisionBody
      if (
        body.parent_revision_id !== state.currentRevisionId ||
        body.base_product_version !== 7 ||
        typeof body.title !== 'string'
      ) {
        await reject(route, 422, 'MANUAL_REVISION_INVALID')
        return
      }
      const nextId = `revision-${state.nextRevisionNumber}`
      const next = revision(nextId, state.nextRevisionNumber, body.title, state.currentRevisionId)
      next.created_by = actor.id
      state.revisions.set(nextId, next)
      state.activeManualRevisionId = nextId
      state.activeManualWorkflowId = `manual-${state.nextRevisionNumber}`
      if (!state.firstManualRevisionId) state.firstManualRevisionId = nextId
      state.nextRevisionNumber += 1
      await respond(route, {
        revision_id: nextId,
        manual_review_workflow_run_id: state.activeManualWorkflowId,
        status: 'accepted',
      }, 202)
      return
    }
    if (path.startsWith('/workflow-runs/manual-') && method === 'GET') {
      if (path !== `/workflow-runs/${state.activeManualWorkflowId}` || !state.activeManualRevisionId) {
        await reject(route, 404, 'WORKFLOW_NOT_FOUND')
        return
      }
      state.currentRevisionId = state.activeManualRevisionId
      state.activeManualRevisionId = null
      state.activeManualWorkflowId = null
      state.proposalStatus = 'draft_ready'
      await respond(route, {
        id: path.split('/').at(-1),
        workflow_type: 'manual_review',
        store_id: store.id,
        start_date: null,
        end_date: null,
        status: 'completed',
        quality_status: 'normal',
        current_step: 'completed',
        attempt_count: 1,
        candidates_ready: false,
        error_code: null,
      })
      return
    }
    if (path === '/proposals/proposal-1/submit' && method === 'POST') {
      if (state.proposalStatus !== 'draft_ready' || writeBody(request).revision_id !== state.currentRevisionId) {
        await reject(route, 409, 'SUBMIT_NOT_ALLOWED')
        return
      }
      state.proposalStatus = 'pending_approval'
      state.submittedRevisionId = state.currentRevisionId
      state.latestAction = action(actor, 'submit', state.currentRevisionId, null)
      await respond(route, state.latestAction, 201)
      return
    }
    if (path === '/approvals' && method === 'GET') {
      if (actor.role === 'operator') {
        await reject(route, 403, 'APPROVAL_FORBIDDEN')
        return
      }
      const items = state.proposalExists && state.proposalStatus === 'pending_approval' && state.submittedRevisionId
        ? [{
            proposal_id: 'proposal-1',
            proposal_revision_id: state.submittedRevisionId,
            revision_number: currentRevision().revision_number,
            store_id: store.id,
            product_id: 'product-1',
            submitted_by: 'operator-1',
            status: 'pending_approval',
            submitted_at: '2026-09-03T08:00:00Z',
          }]
        : []
      await respond(route, { items, page: 1, page_size: 20, total: items.length })
      return
    }
    if (path.startsWith('/approvals/proposal-1/') && method === 'POST') {
      if ((actor.role !== 'supervisor' && actor.role !== 'admin') || state.proposalStatus !== 'pending_approval') {
        await reject(route, 403, 'APPROVAL_FORBIDDEN')
        return
      }
      const body = writeBody(request)
      if (body.revision_id !== state.submittedRevisionId) {
        await reject(route, 409, 'APPROVAL_REVISION_CONFLICT')
        return
      }
      if (path.endsWith('/request-changes')) {
        if (typeof body.comment !== 'string' || body.comment.length < 1) {
          await reject(route, 422, 'APPROVAL_COMMENT_REQUIRED')
          return
        }
        state.proposalStatus = 'pending_manual'
        state.submittedRevisionId = null
        state.latestAction = action(actor, 'request_changes', state.currentRevisionId, body.comment)
        await respond(route, state.latestAction, 201)
        return
      }
      if (path.endsWith('/reject')) {
        if (typeof body.comment !== 'string' || body.comment.length < 1) {
          await reject(route, 422, 'APPROVAL_COMMENT_REQUIRED')
          return
        }
        state.proposalStatus = 'rejected'
        state.latestAction = action(actor, 'reject', state.currentRevisionId, body.comment)
        await respond(route, state.latestAction, 201)
        return
      }
      if (path.endsWith('/approve')) {
        state.proposalStatus = 'completed'
        state.latestAction = action(actor, 'approve', state.currentRevisionId, null)
        state.publishRecord = {
          id: 'publish-1',
          proposal_id: 'proposal-1',
          proposal_revision_id: state.currentRevisionId,
          product_id: 'product-1',
          store_id: store.id,
          approved_by: actor.id,
          approval_action_id: state.latestAction.id,
          before_snapshot: { title: '原商品标题', selling_points: ['原卖点'], description: '原始详情', search_keywords: ['原关键词'], attributes: { 材质: '棉' } },
          after_snapshot: { title: currentRevision().proposal_output.title, selling_points: ['舒适透气'], description: '可信商品详情', search_keywords: ['纯棉', '短袖'], attributes: { 材质: '精梳棉' } },
          base_product_version: 7,
          published_product_version: 8,
          published_at: '2026-09-03T08:00:00Z',
          platform_delivery: {
            status: 'succeeded',
            attempt_count: 1,
            external_operation_id: 'operation-1',
            error_code: null,
            completed_at: '2026-09-03T08:00:01Z',
          },
        }
        await respond(route, state.publishRecord)
        return
      }
    }
    await reject(route, 404, 'FIXTURE_ROUTE_NOT_FOUND')
  })

  return {
    seedPendingApproval: () => {
      state.analysisCreated = true
      state.analysisPolls = 1
      state.proposalExists = true
      state.proposalStatus = 'pending_approval'
      state.submittedRevisionId = state.currentRevisionId
    },
    firstManualRevision: () => {
      const item = state.firstManualRevisionId ? state.revisions.get(state.firstManualRevisionId) : null
      return item ? { id: item.id, title: item.proposal_output.title } : null
    },
    assertProtocol: () => {
      if (!state.protectedRequests || !state.checkedWritePaths.size) {
        throw new Error('FIXTURE_PROTOCOL_NOT_EXERCISED')
      }
    },
  }
}
