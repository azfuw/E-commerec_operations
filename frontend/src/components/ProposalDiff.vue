<script setup lang="ts">
import type { OptimizationProposalOutput } from '../types'

defineProps<{ output: OptimizationProposalOutput }>()

function display(value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    return value
      .map((item) =>
        typeof item === 'string'
          ? item
          : typeof item === 'object' && item !== null && 'body' in item
            ? String(item.body)
            : JSON.stringify(item),
      )
      .join('；')
  }
  return JSON.stringify(value)
}
</script>

<template>
  <section class="proposal-diff" data-test="proposal-diff" aria-labelledby="proposal-diff-title">
    <h2 id="proposal-diff-title">方案差异</h2>
    <article v-for="change in output.changes" :key="change.field" class="diff-row">
      <h3>{{ change.field }}</h3>
      <div class="diff-value" data-test="proposal-diff-current">
        <small>当前值</small>
        <p>{{ display(change.current_value) }}</p>
      </div>
      <div class="diff-value suggested" data-test="proposal-diff-suggested">
        <small>建议值</small>
        <p>{{ display(change.suggested_value) }}</p>
      </div>
    </article>

    <div v-if="output.price_suggestions.length || output.sku_suggestions.length" class="readonly-advice">
      <h2>只读建议</h2>
      <article v-if="output.price_suggestions.length" data-test="readonly-price">
        <h3>价格建议 <small>本阶段不会应用</small></h3>
        <p v-for="item in output.price_suggestions" :key="item.target_sku_id">
          {{ item.target_sku_id }}：{{ item.current_price }} → {{ item.suggested_price }}
        </p>
      </article>
      <article v-if="output.sku_suggestions.length" data-test="readonly-sku">
        <h3>SKU 建议 <small>本阶段不会应用</small></h3>
        <p v-for="item in output.sku_suggestions" :key="item.target_sku_id">
          {{ item.current_code }} → {{ item.suggested_code }}
        </p>
      </article>
    </div>
  </section>
</template>

<style scoped>
.proposal-diff,
.readonly-advice {
  display: grid;
  gap: 16px;
}

.proposal-diff h2,
.diff-row h3,
.readonly-advice h3 {
  margin: 0;
}

.diff-row {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
  padding: 16px;
  border: 1px solid #dbe2e8;
  border-radius: var(--app-radius);
  background: var(--app-surface);
}

.diff-value {
  min-width: 0;
  padding: 12px;
  background: #f8fafc;
  overflow-wrap: anywhere;
}

.diff-value.suggested {
  background: #ecfdf5;
}

.diff-value small,
.readonly-advice small {
  color: var(--app-muted);
}

.diff-value p {
  margin: 6px 0 0;
}

.readonly-advice article {
  padding: 16px;
  border-left: 3px solid #94a3b8;
  background: #f8fafc;
}

@media (max-width: 1023px) {
  .diff-row {
    grid-template-columns: 1fr;
  }
}
</style>
