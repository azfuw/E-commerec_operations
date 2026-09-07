<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  createManualRevision,
  getProposal,
  getWorkflowRun,
  submitProposal,
} from '../api'
import { canApprove } from '../capabilities'
import ApprovalActions from '../components/ApprovalActions.vue'
import CompliancePanel from '../components/CompliancePanel.vue'
import InlineError from '../components/InlineError.vue'
import ManualRevisionForm from '../components/ManualRevisionForm.vue'
import ProposalDiff from '../components/ProposalDiff.vue'
import { buildManualRevisionRequest } from '../manualRevision'
import type { ManualRevisionFormValue } from '../manualRevision'
import type { PlatformDelivery, ProposalDetail, WorkflowRun, WorkflowStatus } from '../types'
import { session } from '../session'
import { useSerialPoll } from '../useSerialPoll'

const route = useRoute()
const proposalId = String(route.params.proposalId)
const detail = ref<ProposalDetail | null>(null)
const loading = ref(true)
const pageError = ref('')
const manualError = ref('')
const manualReviewError = ref('')
const submitError = ref('')
const manualPending = ref(false)
const submitPending = ref(false)
const manualKey = ref<string | null>(null)
const submitKey = ref<string | null>(null)
const lastManualForm = ref<ManualRevisionFormValue | null>(null)
const manualWorkflowId = ref<string | null>(null)
const isMobile =
  typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 767px)').matches

const status = computed(() => detail.value?.optimization_run.status)
const manualInProgress = computed(
  () =>
    manualWorkflowId.value !== null ||
    ['accepted', 'processing'].includes(detail.value?.active_manual_review?.status ?? ''),
)
const canEdit = computed(
  () => !isMobile && status.value === 'pending_manual' && !manualInProgress.value,
)
const canSubmit = computed(
  () => !isMobile && status.value === 'draft_ready' && Boolean(detail.value?.current_revision),
)
const showApprovalActions = computed(
  () =>
    Boolean(
      session.user &&
        status.value &&
        detail.value?.current_revision &&
        canApprove(session.user.role, status.value, isMobile),
    ),
)
const publishFields = ['title', 'selling_points', 'description', 'search_keywords', 'attributes']
const platformStatusText: Record<PlatformDelivery['status'], string> = {
  pending: '等待平台投递',
  processing: '正在投递平台',
  succeeded: '平台投递成功',
  failed: '平台投递失败',
}
const platformErrorText: Record<string, string> = {
  PLATFORM_TEMPORARILY_UNAVAILABLE: '平台暂时不可用',
}

function displaySnapshot(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}

const stages = computed(() => {
  const current = stageIndex(status.value)
  return ['数据分析', '优化方案', '人工复核', '主管审批', '模拟发布'].map((label, index) => ({
    label,
    state: index < current ? '已完成' : index === current ? '当前阶段' : '未开始',
  }))
})

function stageIndex(value: WorkflowStatus | undefined): number {
  if (value === 'pending_manual') return 2
  if (value === 'draft_ready') return 2
  if (value === 'pending_approval') return 3
  if (value === 'completed') return 4
  if (value === 'rejected' || value === 'failed') return 3
  return 1
}

async function loadProposal(): Promise<void> {
  pageError.value = ''
  try {
    const result = await getProposal(proposalId)
    detail.value = result
    if (
      result.active_manual_review &&
      ['accepted', 'processing'].includes(result.active_manual_review.status)
    ) {
      manualWorkflowId.value = result.active_manual_review.workflow_run_id
    }
  } catch {
    pageError.value = '方案加载失败，请重试'
  } finally {
    loading.value = false
  }
}

function workflowTerminal(value: WorkflowRun | null): boolean {
  return value !== null && ['completed', 'failed'].includes(value.status)
}

function receiveWorkflow(value: WorkflowRun | null): void {
  if (value && workflowTerminal(value)) {
    manualWorkflowId.value = null
    if (value.status === 'failed') {
      manualReviewError.value = value.error_code ?? 'MANUAL_REVIEW_FAILED'
    } else {
      manualReviewError.value = ''
    }
    void loadProposal()
  }
}

// ponytail: keep a local no-op timer so a later workflow ID works; extend the composable only if wake-up cost becomes measurable.
useSerialPoll(
  (signal) =>
    manualWorkflowId.value
      ? getWorkflowRun(manualWorkflowId.value, signal)
      : Promise.resolve(null),
  () => false,
  receiveWorkflow,
)

async function writeManual(form: ManualRevisionFormValue): Promise<void> {
  if (!detail.value || manualPending.value) return
  lastManualForm.value = form
  manualReviewError.value = ''
  const built = buildManualRevisionRequest(detail.value, form)
  if (!built.request) {
    manualError.value = '当前方案缺少可修订版本'
    return
  }
  if (!manualKey.value) manualKey.value = globalThis.crypto.randomUUID()
  manualPending.value = true
  manualError.value = ''
  try {
    const accepted = await createManualRevision(proposalId, built.request, manualKey.value)
    manualKey.value = null
    manualWorkflowId.value = accepted.manual_review_workflow_run_id
  } catch {
    manualError.value = '人工修订提交失败，请使用同一请求重试'
  } finally {
    manualPending.value = false
  }
}

function retryManual(): void {
  if (lastManualForm.value) void writeManual(lastManualForm.value)
}

async function writeSubmission(): Promise<void> {
  const revisionId = detail.value?.current_revision?.id
  if (!revisionId || submitPending.value) return
  if (!submitKey.value) submitKey.value = globalThis.crypto.randomUUID()
  submitPending.value = true
  submitError.value = ''
  try {
    await submitProposal(proposalId, revisionId, submitKey.value)
    submitKey.value = null
    await loadProposal()
  } catch {
    submitError.value = '方案提交失败，请使用同一请求重试'
  } finally {
    submitPending.value = false
  }
}

onMounted(() => void loadProposal())
</script>

<template>
  <section aria-labelledby="proposal-title">
    <header class="page-heading">
      <p class="eyebrow">PROPOSAL</p>
      <h1 id="proposal-title">优化方案</h1>
      <p class="muted">方案编号 {{ proposalId }}</p>
    </header>

    <div v-if="loading" data-test="proposal-loading">
      <el-skeleton :rows="8" animated />
    </div>
    <InlineError v-else-if="pageError && !detail" :message="pageError" />
    <template v-else-if="detail">
      <div v-if="pageError" class="write-error" data-test="proposal-refresh-error">
        <InlineError :message="pageError" />
        <el-button data-test="retry-proposal" @click="loadProposal">重新加载</el-button>
      </div>
      <ol class="timeline" aria-label="方案流程">
        <li v-for="stage in stages" :key="stage.label" data-test="timeline-stage">
          <strong>{{ stage.label }}</strong>
          <small>{{ stage.state }}</small>
        </li>
      </ol>

      <div v-if="!detail.current_revision" class="partial" data-test="proposal-partial">
        <p>当前方案尚未生成；以下仅展示服务端已有状态。</p>
      </div>
      <template v-else>
        <ProposalDiff :output="detail.current_revision.proposal_output" />
        <CompliancePanel :review="detail.current_review" />

        <section class="citations" aria-labelledby="citations-title">
          <h2 id="citations-title">可信引用</h2>
          <p
            v-for="citation in detail.current_revision.citations"
            :key="citation.chunk_id"
            :data-test="`citation-${citation.chunk_id}`"
          >
            {{ citation.document_name }} · {{ citation.canonical_text }}
          </p>
        </section>

        <p v-if="manualInProgress" data-test="manual-processing">
          人工复核处理中，完成前不可再次提交修订。
        </p>
        <p v-if="manualReviewError" class="inline-error" data-test="manual-review-failed">
          人工复核失败：{{ manualReviewError }}
        </p>
        <ManualRevisionForm
          v-if="canEdit"
          :key="detail.current_revision.id"
          :detail="detail"
          :disabled="manualPending"
          @submit="writeManual"
        />
        <div v-if="manualError" class="write-error">
          <InlineError :message="manualError" />
          <el-button data-test="retry-manual" :disabled="manualPending" @click="retryManual">
            重新提交人工修订
          </el-button>
        </div>

        <div v-if="canSubmit" class="submit-panel">
          <el-button
            type="primary"
            data-test="submit-proposal"
            :disabled="submitPending"
            :loading="submitPending"
            @click="writeSubmission"
          >提交审批</el-button>
        </div>
        <div v-if="submitError" class="write-error">
          <InlineError :message="submitError" />
          <el-button data-test="retry-submit" :disabled="submitPending" @click="writeSubmission">
            重新提交审批
          </el-button>
        </div>
        <p v-if="status === 'pending_approval'" data-test="pending-reason">
          等待主管或管理员审批，当前方案不可编辑。
        </p>
        <ApprovalActions
          v-if="showApprovalActions && session.user"
          :proposal-id="detail.proposal.id"
          :revision-id="detail.current_revision.id"
          :actor-role="session.user.role"
          :status="detail.optimization_run.status"
          :pending="false"
          @success="loadProposal"
        />

        <section v-if="detail.publish_record" class="publish-record" data-test="publish-record">
          <h2>本地模拟发布结果</h2>
          <p>
            商品版本 {{ detail.publish_record.base_product_version }} →
            {{ detail.publish_record.published_product_version }}
          </p>
          <div class="publish-diff">
            <div data-test="publish-before">
              <h3>发布前</h3>
              <p v-for="field in publishFields" :key="field">
                {{ field }}：{{ displaySnapshot(detail.publish_record.before_snapshot[field]) }}
              </p>
            </div>
            <div data-test="publish-after">
              <h3>发布后</h3>
              <p v-for="field in publishFields" :key="field">
                {{ field }}：{{ displaySnapshot(detail.publish_record.after_snapshot[field]) }}
              </p>
            </div>
          </div>
          <p><strong>价格、SKU、库存与真实平台均未变化。</strong></p>
          <div
            v-if="detail.publish_record.platform_delivery"
            class="platform-delivery"
            data-test="platform-delivery"
          >
            <h3>平台投递状态</h3>
            <p>投递目标：合同模拟器</p>
            <p>{{ platformStatusText[detail.publish_record.platform_delivery.status] }}</p>
            <p>尝试次数 {{ detail.publish_record.platform_delivery.attempt_count }}</p>
            <p v-if="detail.publish_record.platform_delivery.external_operation_id">
              外部操作编号 {{ detail.publish_record.platform_delivery.external_operation_id }}
            </p>
            <p v-if="detail.publish_record.platform_delivery.completed_at">
              完成时间 {{ detail.publish_record.platform_delivery.completed_at }}
            </p>
            <p
              v-if="detail.publish_record.platform_delivery.error_code"
              data-test="platform-delivery-error"
            >
              {{ platformErrorText[detail.publish_record.platform_delivery.error_code] ?? '平台投递失败' }}
            </p>
          </div>
        </section>
      </template>
    </template>
  </section>
</template>

<style scoped>
.timeline {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  padding: 0;
  list-style: none;
}

.timeline li,
.partial,
.citations,
.submit-panel {
  padding: 16px;
  border: 1px solid #dbe2e8;
  border-radius: var(--app-radius);
  background: var(--app-surface);
}

.timeline li {
  display: grid;
  gap: 6px;
}

.timeline small {
  color: var(--app-muted);
}

.citations,
.write-error {
  margin-top: 20px;
}

.citations h2 {
  margin-top: 0;
}

.write-error {
  display: flex;
  align-items: center;
  gap: 12px;
}

.submit-panel {
  margin-top: 20px;
}

.publish-record {
  margin-top: 20px;
  padding: 20px;
  border: 1px solid #86a69f;
  background: #f0fdfa;
}

.publish-record h2,
.publish-record h3 {
  margin-top: 0;
}

.platform-delivery {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid #86a69f;
}

.publish-diff {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  overflow-wrap: anywhere;
}

@media (max-width: 767px) {
  .timeline {
    grid-template-columns: 1fr;
  }

  .publish-diff {
    grid-template-columns: 1fr;
  }
}
</style>
