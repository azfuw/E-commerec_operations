<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { listStores, listWorkbenchTasks } from '../api'
import InlineError from '../components/InlineError.vue'
import StatusTag from '../components/StatusTag.vue'
import { workbenchAction, workflowQuality, workflowStatus } from '../status'
import type {
  StoreSummary,
  TaskKind,
  WorkbenchTask,
  WorkflowStatus,
} from '../types'

const route = useRoute()
const items = ref<WorkbenchTask[]>([])
const stores = ref<StoreSummary[]>([])
const page = ref(1)
const pageSize = 20
const total = ref(0)
const storeId = ref('')
const kind = ref<TaskKind | ''>('')
const status = ref<WorkflowStatus | ''>('')
const loading = ref(false)
const errorMessage = ref('')
let taskController: AbortController | null = null
let storeController: AbortController | null = null

const proposalOnly = computed(() => route.name === 'proposals')
const hasFilters = computed(
  () => proposalOnly.value || Boolean(storeId.value || kind.value || status.value),
)
const storeNames = computed(
  () => new Map(stores.value.map((store) => [store.id, store.name])),
)
const summary = computed(() => ({
  needsAction: items.value.filter((item) => item.requires_current_user_action).length,
  running: items.value.filter((item) => ['accepted', 'processing'].includes(item.status))
    .length,
  failed: items.value.filter((item) => item.status === 'failed').length,
  completed: items.value.filter((item) => item.status === 'completed').length,
}))

const statusOptions = Object.entries(workflowStatus) as [WorkflowStatus, { label: string }][]

function taskPath(task: WorkbenchTask): string {
  if (task.kind === 'proposal' && task.proposal_id) {
    return `/proposals/${encodeURIComponent(task.proposal_id)}`
  }
  return `/analysis/${encodeURIComponent(task.analysis_run_id)}`
}

function formatUpdatedAt(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

async function loadTasks(): Promise<void> {
  taskController?.abort()
  const controller = new AbortController()
  taskController = controller
  loading.value = true
  errorMessage.value = ''
  try {
    const result = await listWorkbenchTasks(
      {
        page: page.value,
        pageSize,
        ...(storeId.value ? { storeId: storeId.value } : {}),
        ...(proposalOnly.value
          ? { kind: 'proposal' as const }
          : kind.value
            ? { kind: kind.value }
            : {}),
        ...(status.value ? { status: status.value } : {}),
      },
      controller.signal,
    )
    items.value = result.items
    total.value = result.total
  } catch {
    if (controller.signal.aborted) return
    items.value = []
    total.value = 0
    errorMessage.value = '任务加载失败，请重试'
  } finally {
    if (taskController === controller) loading.value = false
  }
}

async function loadStoreOptions(): Promise<void> {
  storeController?.abort()
  const controller = new AbortController()
  storeController = controller
  try {
    stores.value = await listStores(controller.signal)
  } catch {
    if (!controller.signal.aborted) errorMessage.value = '任务加载失败，请重试'
  }
}

function changePage(nextPage: number): void {
  page.value = nextPage
  void loadTasks()
}

watch([storeId, kind, status, () => route.name], () => {
  page.value = 1
  void loadTasks()
})

onMounted(() => {
  void loadStoreOptions()
  void loadTasks()
})

onBeforeUnmount(() => {
  taskController?.abort()
  storeController?.abort()
})
</script>

<template>
  <section aria-labelledby="workbench-title">
    <header class="page-heading workbench-heading">
      <div>
        <p class="eyebrow">OPERATIONS</p>
        <h1 id="workbench-title">{{ proposalOnly ? '优化任务' : '任务工作台' }}</h1>
        <p class="muted">从服务端恢复并继续处理你的业务任务。</p>
      </div>
      <span class="current-page-label" data-test="summary-label">当前页</span>
    </header>

    <div class="task-summary" aria-label="当前页任务统计">
      <div class="summary-item" data-test="needs-action-count">
        <strong>{{ summary.needsAction }}</strong><span>待我处理</span>
      </div>
      <div class="summary-item"><strong>{{ summary.running }}</strong><span>运行中</span></div>
      <div class="summary-item"><strong>{{ summary.failed }}</strong><span>异常</span></div>
      <div class="summary-item"><strong>{{ summary.completed }}</strong><span>最近完成</span></div>
    </div>

    <div class="task-filters" aria-label="任务筛选">
      <el-select
        v-model="storeId"
        data-test="store-filter"
        clearable
        placeholder="全部店铺"
        aria-label="店铺"
      >
        <el-option
          v-for="store in stores"
          :key="store.id"
          :label="store.name"
          :value="store.id"
        />
      </el-select>
      <el-select
        v-if="!proposalOnly"
        v-model="kind"
        data-test="kind-filter"
        clearable
        placeholder="全部任务"
        aria-label="任务类型"
      >
        <el-option label="经营分析" value="analysis" />
        <el-option label="优化方案" value="proposal" />
      </el-select>
      <el-select
        v-model="status"
        data-test="status-filter"
        clearable
        placeholder="全部状态"
        aria-label="状态"
      >
        <el-option
          v-for="option in statusOptions"
          :key="option[0]"
          :label="option[1].label"
          :value="option[0]"
        />
      </el-select>
    </div>

    <div v-if="loading" class="workbench-state" data-test="workbench-loading">
      <el-skeleton :rows="4" animated />
      <span class="muted">正在加载任务</span>
    </div>
    <div v-else-if="errorMessage" class="workbench-state" data-test="workbench-error">
      <InlineError :message="errorMessage" />
      <el-button data-test="retry-workbench" @click="loadTasks">重试</el-button>
    </div>
    <el-empty
      v-else-if="!items.length"
      :data-test="hasFilters ? 'filtered-empty' : 'system-empty'"
      :description="hasFilters ? '没有符合筛选条件的任务' : '当前没有任务'"
    />
    <template v-else>
      <el-table class="desktop-task-table" :data="items" table-layout="fixed">
        <el-table-column label="任务详情">
          <template #default="scope">
            <div class="desktop-task-row" :data-test="`task-${scope.row.id}`">
              <span>
                <small>店铺</small>
                {{ storeNames.get(scope.row.store_id) ?? scope.row.store_id }}
              </span>
              <span><small>商品</small>{{ scope.row.product_id ?? '待选品' }}</span>
              <span><small>当前阶段</small><StatusTag :status="scope.row.status" /></span>
              <span>
                <small>质量状态</small>
                <el-tag :type="workflowQuality[scope.row.quality_status].tag" effect="light">
                  {{ workflowQuality[scope.row.quality_status].label }}
                </el-tag>
              </span>
              <span><small>下一动作</small>{{ workbenchAction[scope.row.action_required] }}</span>
              <span><small>更新时间</small>{{ formatUpdatedAt(scope.row.updated_at) }}</span>
              <router-link
                :data-test="`continue-${scope.row.id}`"
                :to="taskPath(scope.row)"
              >继续处理</router-link>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="mobile-task-list" aria-label="移动端任务摘要">
        <article
          v-for="task in items"
          :key="task.id"
          class="mobile-task-row"
          :data-test="`mobile-task-${task.id}`"
        >
          <div class="mobile-task-heading">
            <strong>{{ storeNames.get(task.store_id) ?? task.store_id }}</strong>
            <StatusTag :status="task.status" />
          </div>
          <p>
            {{ workflowQuality[task.quality_status].label }} ·
            {{ workbenchAction[task.action_required] }}
          </p>
          <router-link :to="taskPath(task)">继续处理</router-link>
        </article>
      </div>

      <el-pagination
        data-test="task-pagination"
        background
        layout="prev, pager, next"
        :current-page="page"
        :page-size="pageSize"
        :total="total"
        @current-change="changePage"
      />
    </template>
  </section>
</template>

<style scoped>
.workbench-heading,
.task-filters,
.mobile-task-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.current-page-label,
.desktop-task-row small {
  color: var(--app-muted);
  font-size: 12px;
}

.task-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-bottom: 20px;
  border: 1px solid #dbe2e8;
  border-radius: var(--app-radius);
  background: var(--app-surface);
}

.summary-item {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 16px 20px;
  border-right: 1px solid #dbe2e8;
}

.summary-item:last-child {
  border-right: 0;
}

.summary-item strong {
  font-size: 24px;
}

.summary-item span {
  color: var(--app-muted);
  font-size: 13px;
}

.task-filters {
  justify-content: flex-start;
  margin-bottom: 16px;
}

.task-filters .el-select {
  width: 180px;
}

.workbench-state {
  display: grid;
  gap: 16px;
  justify-items: start;
  padding: 32px 0;
}

.desktop-task-row {
  display: grid;
  grid-template-columns: minmax(120px, 1.2fr) minmax(100px, 1fr) 110px 110px minmax(100px, 1fr) 110px 76px;
  align-items: center;
  gap: 16px;
}

.desktop-task-row > span {
  display: grid;
  gap: 6px;
  min-width: 0;
  overflow-wrap: anywhere;
}

.desktop-task-row a,
.mobile-task-row a {
  color: #0b5f59;
  font-weight: 600;
  text-decoration: none;
}

.mobile-task-list {
  display: none;
}

.el-pagination {
  justify-content: flex-end;
  margin-top: 18px;
}

@media (max-width: 767px) {
  .task-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .summary-item:nth-child(2) {
    border-right: 0;
  }

  .summary-item:nth-child(-n + 2) {
    border-bottom: 1px solid #dbe2e8;
  }

  .task-filters {
    display: grid;
    grid-template-columns: 1fr;
  }

  .task-filters .el-select {
    width: 100%;
  }

  .desktop-task-table {
    display: none;
  }

  .mobile-task-list {
    display: grid;
    gap: 12px;
  }

  .mobile-task-row {
    min-width: 0;
    padding: 16px;
    border: 1px solid #dbe2e8;
    border-radius: var(--app-radius);
    background: var(--app-surface);
    overflow-wrap: anywhere;
  }

  .mobile-task-row p {
    color: var(--app-muted);
    font-size: 14px;
  }
}
</style>
