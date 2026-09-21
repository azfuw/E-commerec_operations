<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { homeLocation } from '../capabilities'
import { session } from '../session'

const route = useRoute()
const destination = computed(() => session.user ? homeLocation(session.user, route.query.workspace) : { name: 'login' })
</script>

<template>
  <section class="page-state">
    <h1>请使用桌面或平板访问管理模块</h1>
    <router-link :to="destination">返回工作台</router-link>
  </section>
</template>
