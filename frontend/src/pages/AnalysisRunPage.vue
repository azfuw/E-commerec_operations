<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'

import { getWorkflowRun, listAnalysisCandidates, selectProduct } from '../api'
import { canSelectProduct } from '../capabilities'
import EvidenceDrawer from '../components/EvidenceDrawer.vue'
import InlineError from '../components/InlineError.vue'
import StatusTag from '../components/StatusTag.vue'
import { session } from '../session'
import type { AnalysisCandidate, WorkflowRun } from '../types'
import { useSerialPoll } from '../useSerialPoll'

const route = useRoute()
const router = useRouter()
const runId = String(route.params.runId)
const workflow = ref<WorkflowRun | null>(null)
const candidates = ref<AnalysisCandidate[]>([])
const candidatesLoading = ref(false)
const pageError = ref('')
const selectionError = ref('')
const selectionKey = ref<string | null>(null)
const selectedCandidateId = ref<string | null>(null)
const submittingCandidateId = ref<string | null>(null)
const isMobile =
  typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 767px)').matches
const canSelect = computed(() =>
  Boolean(
    session.user &&
      workflow.value?.status === 'awaiting_selection' &&
      canSelectProduct(session.user.role, session.user.department, isMobile),
  ),
)

function isTerminal(value: WorkflowRun): boolean {
  return ['awaiting_selection', 'completed', 'failed'].includes(value.status)
}

async function loadCandidates(): Promise<void> {
  candidatesLoading.value = true
  pageError.value = ''
  try {
    candidates.value = await listAnalysisCandidates(runId)
  } catch {
    pageError.value = '候选商品加载失败，请重试'
  } finally {
    candidatesLoading.value = false
  }
}

function receiveWorkflow(value: WorkflowRun): void {
  workflow.value = value
  if (value.status === 'awaiting_selection' && value.candidates_ready) {
    void loadCandidates()
  }
}

useSerialPoll(
  (signal) => getWorkflowRun(runId, signal),
  isTerminal,
  receiveWorkflow,
)

async function submitSelection(candidate: AnalysisCandidate, confirm: boolean): Promise<void> {
  if (submittingCandidateId.value !== null) return
  submittingCandidateId.value = candidate.id
  if (confirm) {
    try {
      await ElMessageBox.confirm(
        `确认选择商品 ${candidate.product_code} 进入优化流程？`,
        '确认选品',
        { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      if (submittingCandidateId.value === candidate.id) submittingCandidateId.value = null
      return
    }
  }
  if (submittingCandidateId.value !== candidate.id) return

  if (selectedCandidateId.value !== candidate.id || !selectionKey.value) {
    selectedCandidateId.value = candidate.id
    selectionKey.value = globalThis.crypto.randomUUID()
  }
  selectionError.value = ''
  try {
    const result = await selectProduct(runId, candidate.id, selectionKey.value)
    selectionKey.value = null
    selectedCandidateId.value = null
    await router.push(`/proposals/${encodeURIComponent(result.proposal_id)}`)
  } catch {
    selectionError.value = '选品提交失败，请使用同一请求重试'
  } finally {
    if (submittingCandidateId.value === candidate.id) submittingCandidateId.value = null
  }
}

function retrySelection(): void {
  const candidate = candidates.value.find((item) => item.id === selectedCandidateId.value)
  if (candidate) void submitSelection(candidate, false)
}
</script>

<template>
  <section aria-labelledby="analysis-run-title">
    <header class="page-heading">
      <p class="eyebrow">ANALYSIS RUN</p>
      <h1 id="analysis-run-title">分析任务</h1>
      <p class="muted">任务编号 {{ runId }}</p>
    </header>

    <div v-if="!workflow || workflow.status === 'accepted' || workflow.status === 'processing'" data-test="analysis-processing">
      <div class="run-state-heading">
        <StatusTag v-if="workflow" :status="workflow.status" />
        <span>处理中，完成后自动刷新候选商品</span>
      </div>
      <el-skeleton :rows="5" animated />
    </div>

    <div v-else-if="workflow.status === 'failed'" class="run-failure">
      <StatusTag status="failed" />
      <InlineError :message="workflow.error_code ?? '分析任务失败'" />
    </div>

    <div v-else-if="workflow.status === 'completed'" class="run-complete">
      <StatusTag status="completed" />
      <p>分析任务已完成。</p>
    </div>

    <div v-else class="candidate-section">
      <div class="candidate-heading">
        <div>
          <h2>候选商品</h2>
          <p class="muted">指标为服务端返回的原始值，不进行趋势推断。</p>
        </div>
        <StatusTag status="awaiting_selection" />
      </div>

      <InlineError v-if="pageError" :message="pageError" />
      <el-skeleton v-if="candidatesLoading" :rows="5" animated />
      <el-empty v-else-if="!candidates.length" description="当前没有候选商品" />
      <template v-else>
        <el-table class="desktop-candidates" :data="candidates" table-layout="fixed">
          <el-table-column label="候选详情">
            <template #default="scope">
              <div class="candidate-row" :data-test="`candidate-${scope.row.id}`">
                <span><small>排名</small>#{{ scope.row.rank }}</span>
                <span><small>商品</small>{{ scope.row.product_code }}</span>
                <span><small>点击率</small>{{ scope.row.metrics.ctr }}</span>
                <span><small>转化率</small>{{ scope.row.metrics.conversion_rate }}</span>
                <span><small>业务影响</small>{{ scope.row.business_impact }}</span>
                <span><small>置信度</small>{{ scope.row.confidence }}</span>
                <EvidenceDrawer :candidate-id="scope.row.id" :evidence="scope.row.evidence" />
                <el-button
                  v-if="canSelect"
                  :data-test="`select-${scope.row.id}`"
                  type="primary"
                  :loading="submittingCandidateId === scope.row.id"
                  :disabled="submittingCandidateId !== null"
                  @click="submitSelection(scope.row, true)"
                >选择商品</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>

        <div class="mobile-candidates" aria-label="移动端候选商品摘要">
          <article
            v-for="candidate in candidates"
            :key="candidate.id"
            class="mobile-candidate"
            :data-test="`mobile-candidate-${candidate.id}`"
          >
            <div class="candidate-heading">
              <strong>#{{ candidate.rank }} {{ candidate.product_code }}</strong>
              <span>置信度 {{ candidate.confidence }}</span>
            </div>
            <p>点击率 {{ candidate.metrics.ctr }} · 转化率 {{ candidate.metrics.conversion_rate }}</p>
            <p>{{ candidate.reason }}</p>
            <EvidenceDrawer :candidate-id="`mobile-${candidate.id}`" :evidence="candidate.evidence" />
          </article>
        </div>
      </template>

      <div v-if="selectionError" class="selection-error" data-test="selection-error">
        <InlineError :message="selectionError" />
        <el-button data-test="retry-selection" :loading="submittingCandidateId !== null" @click="retrySelection">
          重新提交
        </el-button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.run-state-heading,
.candidate-heading,
.selection-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.run-state-heading,
.run-failure,
.run-complete,
.candidate-section {
  display: grid;
  gap: 20px;
}

.candidate-heading h2 {
  margin: 0;
  font-size: 20px;
}

.candidate-row {
  display: grid;
  grid-template-columns: 64px minmax(100px, 1.2fr) 90px 90px 100px 90px 84px 92px;
  align-items: center;
  gap: 14px;
}

.candidate-row > span {
  display: grid;
  gap: 5px;
  min-width: 0;
  overflow-wrap: anywhere;
}

.candidate-row small {
  color: var(--app-muted);
  font-size: 12px;
}

.mobile-candidates {
  display: none;
}

@media (max-width: 767px) {
  .desktop-candidates {
    display: none;
  }

  .mobile-candidates {
    display: grid;
    gap: 12px;
  }

  .mobile-candidate {
    min-width: 0;
    padding: 16px;
    border: 1px solid #dbe2e8;
    border-radius: var(--app-radius);
    background: var(--app-surface);
    overflow-wrap: anywhere;
  }

  .mobile-candidate p {
    color: var(--app-muted);
    font-size: 14px;
  }
}
</style>
