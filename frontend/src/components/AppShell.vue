<script setup lang="ts">
import { House, SwitchButton, TrendCharts } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'

import { clearSession, session } from '../session'

const router = useRouter()

function logout(): void {
  clearSession()
  void router.replace({ name: 'login' })
}
</script>

<template>
  <div class="app-shell">
    <aside class="app-sidebar" aria-label="主导航">
      <div class="app-brand">智营台</div>
      <nav>
        <router-link class="nav-link" :to="{ name: 'workbench' }">
          <el-icon><House /></el-icon>
          <span>工作台</span>
        </router-link>
        <router-link class="nav-link" :to="{ name: 'analysis' }">
          <el-icon><TrendCharts /></el-icon>
          <span>经营分析</span>
        </router-link>
        <router-link class="nav-link" :to="{ name: 'proposals' }">
          <el-icon><House /></el-icon>
          <span>优化任务</span>
        </router-link>
      </nav>
    </aside>
    <div class="app-workspace">
      <header class="app-header">
        <div v-if="session.user" class="user-summary">
          <span>{{ session.user.username }}</span>
          <span class="muted">{{ session.user.role }}</span>
        </div>
        <el-button :icon="SwitchButton" text @click="logout">退出登录</el-button>
      </header>
      <main class="app-main">
        <router-view />
      </main>
    </div>
  </div>
</template>
