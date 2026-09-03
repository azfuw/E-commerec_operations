<script setup lang="ts">
defineProps<{ message: string }>()
</script>

<template>
  <p class="inline-error" role="alert">{{ message }}</p>
</template>
