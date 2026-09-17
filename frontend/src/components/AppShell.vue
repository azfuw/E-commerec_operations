<script setup lang="ts">
import {
  Box, ChatLineRound, Checked, Collection, DataAnalysis, Document,
  House, RefreshLeft, Setting, SwitchButton, Tickets, TrendCharts, Van, Warning,
} from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { canUseKnowledge, canViewAgentObservability, canViewAuditEvents, canManageSystem } from '../capabilities'
import { getLogisticsView, logisticsLocation, logisticsViewLabels, type LogisticsView } from '../logistics'
import { clearSession, session } from '../session'

const router = useRouter()
const route = useRoute()
const media = typeof window.matchMedia === 'function' ? window.matchMedia('(max-width: 767px)') : null
const isMobile = ref(media?.matches ?? false)
const updateViewport = () => { isMobile.value = media?.matches ?? false }
onMounted(() => media?.addEventListener?.('change', updateViewport))
onBeforeUnmount(() => media?.removeEventListener?.('change', updateViewport))

const isLogistics = computed(() => route.name === 'logistics' || (route.meta.workspace === 'shared' && route.query.workspace === 'logistics'))
const departmentLabel = computed(() => isLogistics.value ? '物流工作台' : '运营工作台')
const lastOperations = ref('/workbench')
const lastLogistics = ref('/logistics')
watch(() => route.fullPath, (path) => {
  if (route.meta.workspace === 'operations') lastOperations.value = path
  if (route.name === 'logistics') lastLogistics.value = path
}, { immediate: true })

const roleLabel = computed(() => session.user ? { operator: '员工', supervisor: '主管', admin: '管理员' }[session.user.role] : '')
const canReview = computed(() => session.user?.role === 'supervisor' || session.user?.role === 'admin')
const logisticsIcons = { overview: DataAnalysis, shipments: Box, returns: RefreshLeft, exceptions: Warning, agent: ChatLineRound }
const logisticsViews = Object.keys(logisticsViewLabels) as LogisticsView[]
const operationLinks = computed(() => [
  { name: 'workbench', label: '任务工作台', icon: House, active: route.name === 'workbench', visible: true },
  { name: 'analysis', label: '经营分析', icon: TrendCharts, active: ['analysis', 'analysis-run'].includes(String(route.name)), visible: !isMobile.value },
  { name: 'proposals', label: '优化任务', icon: Document, active: ['proposals', 'proposal'].includes(String(route.name)), visible: true },
  { name: 'approvals', label: '待审批', icon: Checked, active: route.name === 'approvals', visible: canReview.value },
].filter(item => item.visible))
const sharedLinks = computed(() => {
  const role = session.user?.role
  if (!role) return []
  return [
    { name: 'knowledge', label: '知识库', icon: Collection, visible: canUseKnowledge(role, isMobile.value) },
    { name: 'agent-evaluations', label: 'Agent 评测', icon: DataAnalysis, visible: canViewAgentObservability(role, isMobile.value) },
    { name: 'audit-events', label: '审计日志', icon: Tickets, visible: canViewAuditEvents(role, isMobile.value) },
    { name: 'admin', label: '系统管理', icon: Setting, visible: canManageSystem(role, isMobile.value) },
  ].filter(item => item.visible)
})
const currentPage = computed(() => route.name === 'logistics'
  ? logisticsViewLabels[getLogisticsView(route.query.view)]
  : sharedLinks.value.find(item => item.name === route.name)?.label ?? operationLinks.value.find(item => item.active)?.label ?? '工作台')

function logout(): void {
  clearSession()
  void router.replace({ name: 'login' })
}
</script>

<template>
  <div class="app-shell">
    <a class="skip-link" href="#workspace-content">跳至工作内容</a>
    <aside class="app-sidebar" aria-label="主导航">
      <div class="sidebar-top">
        <div class="app-brand"><span class="brand-symbol" aria-hidden="true">营</span><div><strong>智营台</strong><small>运营与物流协同</small></div></div>
        <nav class="department-switch" aria-label="部门切换">
          <router-link :to="lastOperations" aria-label="运营工作台" :aria-current="!isLogistics ? 'true' : undefined" :class="{ selected: !isLogistics }"><el-icon><TrendCharts /></el-icon><span>运营</span></router-link>
          <router-link :to="lastLogistics" aria-label="物流工作台" :aria-current="isLogistics ? 'true' : undefined" :class="{ selected: isLogistics }"><el-icon><Van /></el-icon><span>物流</span></router-link>
        </nav>
      </div>
      <nav class="business-navigation" :aria-label="isLogistics ? '物流业务导航' : '运营业务导航'">
        <p class="nav-group-label">{{ isLogistics ? '物流业务' : '运营业务' }}</p>
        <div class="nav-items">
          <template v-if="isLogistics">
            <router-link v-for="view in logisticsViews" :key="view" class="nav-link" :data-test="'nav-' + view" :to="logisticsLocation(view)" :class="{ selected: route.name === 'logistics' && getLogisticsView(route.query.view) === view }" :aria-current="route.name === 'logistics' && getLogisticsView(route.query.view) === view ? 'page' : undefined">
              <el-icon><component :is="logisticsIcons[view]" /></el-icon><span>{{ logisticsViewLabels[view] }}</span>
            </router-link>
          </template>
          <template v-else>
            <router-link v-for="item in operationLinks" :key="item.name" class="nav-link" :to="{ name: item.name }" :class="{ selected: item.active }" :aria-current="item.active ? 'page' : undefined"><el-icon><component :is="item.icon" /></el-icon><span>{{ item.label }}</span></router-link>
          </template>
        </div>
      </nav>
      <nav v-if="sharedLinks.length" class="shared-navigation" aria-label="管理与支持">
        <p class="nav-group-label">管理与支持</p>
        <div class="nav-items">
          <router-link v-for="item in sharedLinks" :key="item.name" class="nav-link" :to="{ name: item.name, query: isLogistics ? { workspace: 'logistics' } : {} }" :class="{ selected: route.name === item.name }" :aria-current="route.name === item.name ? 'page' : undefined"><el-icon><component :is="item.icon" /></el-icon><span>{{ item.label }}</span></router-link>
        </div>
      </nav>
    </aside>
    <div class="app-workspace">
      <header class="app-header">
        <div class="workspace-path"><span>{{ route.meta.workspace === 'shared' ? '管理与支持' : departmentLabel }}</span><span aria-hidden="true">/</span><strong>{{ currentPage }}</strong></div>
        <div class="account-controls">
          <div v-if="session.user" class="user-summary"><span class="user-avatar" aria-hidden="true">{{ session.user.username.slice(0, 1).toUpperCase() }}</span><span class="user-name">{{ session.user.username }}</span><span class="user-role">{{ roleLabel }}</span></div>
          <el-button class="logout-button" :icon="SwitchButton" text aria-label="退出登录" @click="logout"><span>退出登录</span></el-button>
        </div>
      </header>
      <main id="workspace-content" class="app-main" tabindex="-1"><router-view /></main>
    </div>
  </div>
</template>

<style scoped>
.app-shell {
  display: grid;
  grid-template-columns: 224px minmax(0, 1fr);
  min-height: 100dvh;
}
.skip-link { position: fixed; top: -64px; left: 16px; z-index: 10; padding: 12px; background: var(--app-surface); color: var(--app-accent); }
.skip-link:focus { top: 12px; }
.app-sidebar {
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  gap: 26px;
  height: 100dvh;
  padding: 28px 16px;
  overflow-y: auto;
  border-right: 1px solid var(--app-border);
  background: var(--app-sidebar);
}
.app-brand { display: flex; align-items: center; gap: 10px; padding: 0 10px; }
.brand-symbol { display: grid; place-items: center; width: 34px; height: 36px; border: 1px solid #c7d7cd; border-radius: 9px; color: var(--app-accent); font-size: 19px; font-weight: 650; }
.app-brand > div { display: grid; gap: 3px; }
.app-brand strong { font-size: 21px; line-height: 1.2; letter-spacing: -.04em; }
.app-brand small { color: var(--app-muted); font-size: 10px; letter-spacing: .08em; }
.department-switch { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin-top: 26px; padding: 4px; border: 1px solid var(--app-border); border-radius: 9px; background: #f0f3ef; }
.department-switch a { display: flex; align-items: center; justify-content: center; gap: 8px; min-height: 34px; border-radius: 6px; color: var(--app-muted); text-decoration: none; transition: background .16s, color .16s; }
.department-switch a:hover { color: var(--app-accent); }
.department-switch a.selected { background: var(--app-surface); color: var(--app-accent); box-shadow: 0 1px 3px #233f2c12; font-weight: 650; }
.nav-group-label { margin: 0 12px 10px; color: var(--app-muted); font-size: 11px; letter-spacing: .06em; }
.nav-items { display: grid; gap: 5px; }
.nav-link { display: flex; align-items: center; gap: 11px; min-height: 42px; padding: 0 12px; border-radius: 7px; color: #59655d; font-size: 14px; text-decoration: none; transition: background .16s, color .16s; }
.nav-link .el-icon { flex-shrink: 0; font-size: 17px; }
.nav-link:hover { background: #eff3ee; color: var(--app-text); }
.nav-link.selected { color: var(--app-accent); background: var(--app-accent-soft); font-weight: 650; }
.shared-navigation { padding-top: 22px; border-top: 1px solid var(--app-border); }
.app-workspace { min-width: 0; }
.app-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; min-height: 68px; padding: 0 32px; border-bottom: 1px solid var(--app-border); background: var(--app-surface); }
.workspace-path { display: flex; align-items: center; gap: 12px; min-width: 0; color: var(--app-muted); font-size: 12px; }
.workspace-path strong { color: var(--app-text); font-weight: 550; }
.account-controls, .user-summary { display: flex; align-items: center; gap: 12px; }
.user-summary { font-size: 13px; }
.user-avatar { display: grid; flex-shrink: 0; place-items: center; width: 30px; height: 30px; border: 1px solid var(--app-border); border-radius: 50%; background: var(--app-bg); color: var(--app-accent); font-weight: 600; }
.user-name { max-width: 140px; overflow: hidden; text-overflow: ellipsis; }
.user-role { color: var(--app-muted); font-size: 11px; }
.logout-button { color: var(--app-muted); }
.app-main { width: 100%; max-width: 1600px; margin: 0 auto; padding: 30px 32px 48px; }
.app-main:focus { outline: none; }
@media (max-width: 1100px) {
  .app-shell { grid-template-columns: 208px minmax(0, 1fr); }
  .app-sidebar { padding-right: 12px; padding-left: 12px; }
  .app-header { padding-right: 24px; padding-left: 24px; }
  .app-main { padding: 24px 24px 40px; }
  .user-role, .logout-button span { display: none; }
}
@media (max-width: 767px) {
  .app-shell { display: block; }
  .app-sidebar { z-index: 5; gap: 12px; height: auto; padding: 12px 16px 10px; overflow: visible; border-right: 0; border-bottom: 1px solid var(--app-border); }
  .sidebar-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .app-brand { padding: 0; }
  .app-brand strong { font-size: 19px; }
  .app-brand small { display: none; }
  .brand-symbol { width: 28px; height: 30px; font-size: 16px; }
  .department-switch { flex-shrink: 0; width: 156px; margin: 0; padding: 3px; }
  .department-switch a { min-height: 32px; gap: 6px; font-size: 13px; }
  .nav-group-label { display: none; }
  .business-navigation { min-width: 0; }
  .nav-items { display: flex; gap: 4px; overflow-x: auto; scrollbar-width: thin; }
  .nav-link { flex: 0 0 auto; min-height: 38px; gap: 7px; padding: 0 10px; font-size: 12px; white-space: nowrap; }
  .nav-link .el-icon { font-size: 15px; }
  .app-header { min-height: 48px; gap: 10px; padding: 0 16px; }
  .workspace-path { gap: 8px; font-size: 11px; }
  .workspace-path > span { display: none; }
  .account-controls, .user-summary { gap: 6px; }
  .user-name { max-width: 90px; font-size: 12px; }
  .user-avatar { width: 24px; height: 24px; font-size: 11px; }
  .logout-button { padding: 8px; }
  .app-main { padding: 22px 16px 32px; }
}
</style>
