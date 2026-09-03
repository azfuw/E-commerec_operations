<script setup lang="ts">
import { computed, reactive } from 'vue'

import { buildManualRevisionRequest } from '../manualRevision'
import type { ManualRevisionFormValue } from '../manualRevision'
import type { ProposalDetail } from '../types'

const props = defineProps<{
  detail: ProposalDetail
  disabled: boolean
}>()
const emit = defineEmits<{ submit: [value: ManualRevisionFormValue] }>()
const revision = props.detail.current_revision
if (!revision) throw new Error('CURRENT_REVISION_REQUIRED')
const output = revision.proposal_output

const form = reactive<ManualRevisionFormValue>({
  title: output.title,
  selling_points: [...output.selling_points],
  description: output.description.map((section) => ({
    ...section,
    evidence: section.evidence.map((item) => ({ ...item })),
  })),
  keywords: [...output.keywords],
  attribute_completions: output.attribute_completions.slice(0, 20).map((item) => ({
    target_attribute: item.target_attribute,
    suggested_value: item.suggested_value,
  })),
})
const unsupported = computed(() => buildManualRevisionRequest(props.detail, form).unsupported)

const valid = computed(
  () =>
    form.title.length >= 1 &&
    form.title.length <= 60 &&
    /[\u4e00-\u9fff]/u.test(form.title) &&
    form.selling_points.length >= 1 &&
    form.selling_points.length <= 5 &&
    form.selling_points.every((item) => item.length >= 1 && item.length <= 80) &&
    form.description.length >= 1 &&
    form.description.length <= 10 &&
    form.description.every(
      (item) =>
        item.heading.length >= 1 &&
        item.heading.length <= 40 &&
        item.body.length >= 1 &&
        item.body.length <= 1000,
    ) &&
    form.keywords.length >= 1 &&
    form.keywords.length <= 20 &&
    form.keywords.every((item) => item.length >= 1 && item.length <= 32) &&
    form.attribute_completions.every(
      (item) => item.target_attribute.length >= 1 && item.suggested_value.length >= 1,
    ),
)

function submit(): void {
  emit('submit', {
    title: form.title,
    selling_points: [...form.selling_points],
    description: form.description.map((section) => ({
      ...section,
      evidence: section.evidence.map((item) => ({ ...item })),
    })),
    keywords: [...form.keywords],
    attribute_completions: form.attribute_completions.map((item) => ({ ...item })),
  })
}
</script>

<template>
  <section class="manual-form" aria-labelledby="manual-form-title">
    <h2 id="manual-form-title">人工修订</h2>
    <label data-test="manual-title">
      <span>标题</span>
      <el-input
        v-model="form.title"
        maxlength="60"
        show-word-limit
        :disabled="disabled || unsupported.includes('title')"
      />
      <small v-if="unsupported.includes('title')" data-test="unsupported-title">
        缺少可信元数据，此字段保持只读
      </small>
    </label>
    <label>
      <span>卖点（每行一项，最多 5 项）</span>
      <el-input
        :model-value="form.selling_points.join('\n')"
        type="textarea"
        :rows="3"
        maxlength="404"
        :disabled="disabled || unsupported.includes('selling_points')"
        @update:model-value="form.selling_points = String($event).split('\n').slice(0, 5)"
      />
      <small v-if="unsupported.includes('selling_points')">
        缺少可信元数据，此字段保持只读
      </small>
    </label>
    <label v-for="(section, index) in form.description" :key="index">
      <span>详情 {{ index + 1 }}</span>
      <el-input
        v-model="section.heading"
        maxlength="40"
        :disabled="disabled || unsupported.includes('description')"
      />
      <small v-if="unsupported.includes('description')">
        缺少可信元数据，此字段保持只读
      </small>
      <el-input
        v-model="section.body"
        type="textarea"
        maxlength="1000"
        :disabled="disabled || unsupported.includes('description')"
      />
    </label>
    <label>
      <span>关键词（每行一项，最多 20 项）</span>
      <el-input
        :model-value="form.keywords.join('\n')"
        type="textarea"
        :rows="3"
        maxlength="659"
        :disabled="disabled || unsupported.includes('keywords')"
        @update:model-value="form.keywords = String($event).split('\n').slice(0, 20)"
      />
      <small v-if="unsupported.includes('keywords')">
        缺少可信元数据，此字段保持只读
      </small>
    </label>
    <label v-for="item in form.attribute_completions" :key="item.target_attribute">
      <span>属性 {{ item.target_attribute }}</span>
      <el-input
        v-model="item.suggested_value"
        maxlength="512"
        :disabled="disabled || unsupported.includes('attribute_completions')"
      />
      <small v-if="unsupported.includes('attribute_completions')">
        属性证据不完整，本组保持只读且不提交不可信项
      </small>
    </label>
    <el-button
      type="primary"
      data-test="submit-manual"
      :disabled="disabled || !valid"
      :loading="disabled"
      @click="submit"
    >提交人工修订</el-button>
  </section>
</template>

<style scoped>
.manual-form,
.manual-form label {
  display: grid;
  gap: 10px;
}

.manual-form {
  padding: 20px;
  border: 1px solid #dbe2e8;
  border-radius: var(--app-radius);
  background: var(--app-surface);
}

.manual-form h2 {
  margin: 0;
}

.manual-form label > span {
  font-size: 14px;
  font-weight: 600;
}
</style>
