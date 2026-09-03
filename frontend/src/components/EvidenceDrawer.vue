<script setup lang="ts">
import { ref } from 'vue'

defineProps<{
  candidateId: string
  evidence: string[]
}>()

const open = ref(false)
</script>

<template>
  <el-button
    text
    type="primary"
    :data-test="`open-evidence-${candidateId}`"
    @click="open = true"
  >查看证据</el-button>
  <el-drawer v-model="open" title="候选证据" :append-to-body="false" size="min(420px, 92vw)">
    <ul class="evidence-list">
      <li v-for="item in evidence" :key="item">{{ item }}</li>
    </ul>
  </el-drawer>
</template>

<style scoped>
.evidence-list {
  display: grid;
  gap: 12px;
  margin: 0;
  padding-left: 20px;
  color: var(--app-text);
  line-height: 1.65;
  overflow-wrap: anywhere;
}
</style>
