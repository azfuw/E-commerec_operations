<script setup lang="ts">
import { computed } from 'vue'
import { homeLocation } from '../capabilities'
import { session } from '../session'

const destination = computed(() => session.user ? homeLocation(session.user) : { name: 'login' })
</script>

<template>
  <section class="page-state" role="alert">
    <p class="eyebrow">403</p>
    <h1>没有访问权限</h1>
    <p class="muted">当前账号不能访问此页面。</p>
    <router-link :to="destination">返回工作台</router-link>
  </section>
</template>
