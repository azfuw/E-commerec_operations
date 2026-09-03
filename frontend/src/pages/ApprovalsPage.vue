<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ApiError, listApprovals } from '../api'
import type { ApprovalListItem } from '../types'

const items = ref<ApprovalListItem[]>([])
const page = ref(1)
const pageSize = 20
const total = ref(0)
const loading = ref(true)
const error = ref('')

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await listApprovals(page.value, pageSize)
    items.value = result.items
    total.value = result.total
  } catch (caught) {
    items.value = []
    total.value = 0
    error.value = caught instanceof ApiError && caught.status === 403
      ? '无权查看待审批方案'
      : '待审批方案加载失败'
  } finally {
    loading.value = false
  }
}

function changePage(next: number): void {
  if (next < 1 || next > Math.ceil(total.value / pageSize)) return
  page.value = next
  void load()
}

onMounted(() => void load())
</script>

<template>
  <section aria-labelledby="approvals-title">
    <header class="page-heading">
      <p class="eyebrow">APPROVALS</p>
      <h1 id="approvals-title">待审批方案</h1>
      <p class="muted" data-test="approval-total">共 {{ total }} 项</p>
    </header>

    <el-skeleton v-if="loading" :rows="5" animated />
    <p v-else-if="error" class="inline-error" data-test="approvals-error">{{ error }}</p>
    <div v-else-if="!items.length" data-test="approvals-empty">暂无待审批方案</div>
    <template v-else>
      <article
        v-for="item in items"
        :key="item.proposal_id"
        class="approval-row"
        :data-test="`approval-${item.proposal_id}`"
      >
        <div>
          <strong>商品 {{ item.product_id }}</strong>
          <p>版本 {{ item.revision_number }} · 店铺 {{ item.store_id }}</p>
          <small>提交人 {{ item.submitted_by }} · {{ item.submitted_at }}</small>
        </div>
        <router-link
          :to="{ name: 'proposal', params: { proposalId: item.proposal_id } }"
          :data-test="`open-${item.proposal_id}`"
        >查看并审批</router-link>
      </article>
      <nav class="pagination" aria-label="待审批分页">
        <button type="button" :disabled="page === 1" @click="changePage(page - 1)">上一页</button>
        <span>第 {{ page }} 页</span>
        <button
          type="button"
          data-test="approvals-next"
          :disabled="page * pageSize >= total"
          @click="changePage(page + 1)"
        >下一页</button>
      </nav>
    </template>
  </section>
</template>

<style scoped>
.approval-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 18px;
  border-bottom: 1px solid #dbe2e8;
  background: var(--app-surface);
}

.approval-row p {
  margin: 6px 0;
}

.pagination {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 18px;
}

@media (max-width: 767px) {
  .approval-row {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
