<script setup lang="ts">
import { computed } from 'vue'

import { workflowStatus } from '../status'
import type { WorkflowStatus } from '../types'

const props = defineProps<{ status: WorkflowStatus }>()
const meta = computed(() => workflowStatus[props.status])
</script>

<template>
  <el-tag :type="meta.tag" effect="light">{{ meta.label }}</el-tag>
</template>
