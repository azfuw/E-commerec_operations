import { createRouter, createWebHistory } from 'vue-router'
import { h } from 'vue'
import { canUseKnowledge, canViewAgentObservability, canViewAuditEvents, canManageSystem } from './capabilities'
import SystemManagementPage from './pages/SystemManagementPage.vue'
import KnowledgePage from './pages/KnowledgePage.vue'
import AgentEvaluationsPage from './pages/AgentEvaluationsPage.vue'
import AuditEventsPage from './pages/AuditEventsPage.vue'

import AppShell from './components/AppShell.vue'
import ForbiddenPage from './pages/ForbiddenPage.vue'
import LoginPage from './pages/LoginPage.vue'
import AnalysisPage from './pages/AnalysisPage.vue'
import AnalysisRunPage from './pages/AnalysisRunPage.vue'
import ProposalPage from './pages/ProposalPage.vue'
import ApprovalsPage from './pages/ApprovalsPage.vue'
import WorkbenchPage from './pages/WorkbenchPage.vue'
import { session } from './session'
import { logisticsLocation } from './logistics'

export function createAppRouter() {
  const router = createRouter({
    history: createWebHistory('/app/'),
    scrollBehavior: (_to, _from, savedPosition) => savedPosition ?? { top: 0 },
    routes: [
      { path: '/login', name: 'login', component: LoginPage },
      { path: '/forbidden', name: 'forbidden', component: ForbiddenPage },
      { path: '/desktop-required', name: 'desktop-required', component: { render: () => h('section', {class:'page-state'}, [h('h1','请使用桌面或平板访问管理模块'), h('a',{href:'/app/workbench'},'返回工作台')]) } },
      {
        path: '/',
        component: AppShell,
        meta: { requiresAuth: true, workspace: 'operations' },
        children: [
          { path: '', redirect: { name: 'workbench' } },
          { path: 'logistics', name: 'logistics', component: () => import('./pages/LogisticsPage.vue'), meta: { workspace: 'logistics' } },
          { path: 'workbench', name: 'workbench', component: WorkbenchPage },
          { path: 'knowledge', name: 'knowledge', component: KnowledgePage, meta: { workspace: 'shared' } },
          { path: 'agent-evaluations', name: 'agent-evaluations', component: AgentEvaluationsPage, meta: { workspace: 'shared' } },
          { path: 'audit-events', name: 'audit-events', component: AuditEventsPage, meta: { workspace: 'shared' } },
          { path: 'admin', name: 'admin', component: SystemManagementPage, meta: { workspace: 'shared' } },
          { path: 'analysis', name: 'analysis', component: AnalysisPage },
          { path: 'analysis/:runId', name: 'analysis-run', component: AnalysisRunPage },
          { path: 'proposals', name: 'proposals', component: WorkbenchPage },
          { path: 'proposals/:proposalId', name: 'proposal', component: ProposalPage },
          {
            path: 'approvals',
            name: 'approvals',
            component: ApprovalsPage,
            meta: { approvalOnly: true },
          },
        ],
      },
      { path: '/:pathMatch(.*)*', redirect: { name: 'workbench' } },
    ],
  })

  router.beforeEach((to) => {
    if (to.name === 'login' && session.user) return to.query.next === 'logistics' ? logisticsLocation(to.query.view) : { name: 'workbench' }
    if (to.meta.requiresAuth && !session.user) return { name: 'login', query: to.name === 'logistics' ? { next: 'logistics', ...logisticsLocation(to.query.view).query } : {} }
    const mobile = typeof matchMedia === 'function' && matchMedia('(max-width: 767px)').matches
    if (to.name === 'knowledge' && session.user && !canUseKnowledge(session.user.role, mobile)) {
      return { name: canUseKnowledge(session.user.role, false) ? 'desktop-required' : 'forbidden' }
    }
    if (to.name === 'agent-evaluations' && session.user && !canViewAgentObservability(session.user.role, mobile)) {
      return { name: canViewAgentObservability(session.user.role, false) ? 'desktop-required' : 'forbidden' }
    }
    if (to.name === 'audit-events' && session.user && !canViewAuditEvents(session.user.role, mobile)) {
      return { name: canViewAuditEvents(session.user.role, false) ? 'desktop-required' : 'forbidden' }
    }
    if (to.name === 'admin' && session.user && !canManageSystem(session.user.role, mobile)) {
      return { name: canManageSystem(session.user.role, false) ? 'desktop-required' : 'forbidden' }
    }
    if (
      to.meta.approvalOnly &&
      session.user?.role !== 'supervisor' &&
      session.user?.role !== 'admin'
    ) {
      return { name: 'forbidden' }
    }
  })
  return router
}
