<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { createAnalysisRun, listStores } from '../api'
import { canStartAnalysis } from '../capabilities'
import InlineError from '../components/InlineError.vue'
import { session } from '../session'
import type { StoreSummary } from '../types'

const router = useRouter()
const stores = ref<StoreSummary[]>([])
const storeId = ref('')
const dates = ref<[string, string] | null>(null)
const submitting = ref(false)
const errorMessage = ref('')
const storeController = new AbortController()
const isMobile =
  typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 767px)').matches
const canStart = computed(
  () => Boolean(session.user && canStartAnalysis(session.user.role, isMobile)),
)
const days = computed(() => {
  if (!dates.value) return 0
  return Math.round(
    (Date.parse(dates.value[1]) - Date.parse(dates.value[0])) / 86_400_000,
  ) + 1
})
const validDates = computed(() => days.value >= 1 && days.value <= 90)

async function loadStoreOptions(): Promise<void> {
  try {
    stores.value = await listStores(storeController.signal)
  } catch {
    if (!storeController.signal.aborted) errorMessage.value = '店铺加载失败，请重试'
  }
}

async function submit(): Promise<void> {
  if (!dates.value || !storeId.value || !validDates.value || submitting.value) return
  submitting.value = true
  errorMessage.value = ''
  try {
    const result = await createAnalysisRun({
      store_id: storeId.value,
      start_date: dates.value[0],
      end_date: dates.value[1],
    })
    await router.push(`/analysis/${encodeURIComponent(result.workflow_run_id)}`)
  } catch {
    errorMessage.value = '分析任务创建失败，请重试'
  } finally {
    submitting.value = false
  }
}

onMounted(() => void loadStoreOptions())
onBeforeUnmount(() => storeController.abort())
</script>

<template>
  <section aria-labelledby="analysis-title">
    <header class="page-heading">
      <p class="eyebrow">ANALYSIS</p>
      <h1 id="analysis-title">经营分析</h1>
      <p class="muted">选择授权店铺和日期范围，创建一项可恢复的分析任务。</p>
    </header>

    <el-empty v-if="!canStart" description="当前设备或角色仅可查看已有分析任务" />
    <form v-else class="analysis-form" @submit.prevent="submit">
      <div class="form-field">
        <label for="analysis-store">店铺</label>
        <el-select
          id="analysis-store"
          v-model="storeId"
          data-test="analysis-store"
          placeholder="选择店铺"
          :disabled="submitting"
        >
          <el-option
            v-for="store in stores"
            :key="store.id"
            :label="store.name"
            :value="store.id"
          />
        </el-select>
      </div>
      <div class="form-field">
        <label>分析日期</label>
        <el-date-picker
          v-model="dates"
          type="daterange"
          value-format="YYYY-MM-DD"
          aria-label="分析日期"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          :disabled="submitting"
        />
        <p v-if="dates && !validDates" class="inline-error" data-test="date-error">
          日期范围必须为 1 至 90 天
        </p>
      </div>
      <InlineError v-if="errorMessage" :message="errorMessage" />
      <el-button
        data-test="start-analysis"
        native-type="submit"
        type="primary"
        :loading="submitting"
        :disabled="submitting || !storeId || !validDates"
      >开始分析</el-button>
    </form>
  </section>
</template>

<style scoped>
.analysis-form {
  display: grid;
  gap: 20px;
  max-width: 620px;
  padding: 24px;
  border: 1px solid #dbe2e8;
  border-radius: var(--app-radius);
  background: var(--app-surface);
}

.analysis-form .el-select,
.analysis-form .el-date-editor {
  width: 100%;
}

.analysis-form .el-button {
  justify-self: start;
}
</style>
