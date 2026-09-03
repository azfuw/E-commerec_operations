<script setup lang="ts">
import type { ComplianceReview } from '../types'

defineProps<{ review: ComplianceReview | null }>()
</script>

<template>
  <section class="compliance-panel" aria-labelledby="compliance-title">
    <h2 id="compliance-title">合规结果</h2>
    <p v-if="!review" class="muted">当前尚无合规结果。</p>
    <template v-else>
      <p>
        {{ review.passed ? '已通过' : '未通过' }} · 风险等级 {{ review.risk_level }} ·
        质量 {{ review.quality_status }}
      </p>
      <p v-if="review.error_code" class="inline-error">{{ review.error_code }}</p>
      <ul v-if="review.required_changes.length">
        <li
          v-for="change in review.required_changes"
          :key="`${change.field}-${change.source_violation_code}`"
          :data-test="`required-change-${change.field}`"
        >
          <strong>{{ change.field }}</strong>：{{ change.instruction }}
        </li>
      </ul>
    </template>
  </section>
</template>

<style scoped>
.compliance-panel {
  padding: 20px;
  border: 1px solid #dbe2e8;
  border-radius: var(--app-radius);
  background: var(--app-surface);
}

.compliance-panel h2 {
  margin-top: 0;
}

.compliance-panel li + li {
  margin-top: 8px;
}
</style>
