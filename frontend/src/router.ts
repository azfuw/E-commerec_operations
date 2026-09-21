import { createRouter, createWebHistory } from 'vue-router'
import { canAccessDepartment, canUseKnowledge, canViewAgentObservability, canViewAuditEvents, canManageSystem, homeLocation } from './capabilities'
import SystemManagementPage from './pages/SystemManagementPage.vue'
import KnowledgePage from './pages/KnowledgePage.vue'
import AgentEvaluationsPage from './pages/AgentEvaluationsPage.vue'
import AuditEventsPage from './pages/AuditEventsPage.vue'

import AppShell from './components/AppShell.vue'
import ForbiddenPage from './pages/ForbiddenPage.vue'
import DesktopRequiredPage from './pages/DesktopRequiredPage.vue'
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
      { path: '/desktop-required', name: 'desktop-required', component: DesktopRequiredPage },
      {
        path: '/',
        component: AppShell,
        meta: { requiresAuth: true, workspace: 'operations' },
        children: [
          { path: '', redirect: () => session.user ? homeLocation(session.user) : { name: 'workbench' } },
          { path: 'logistics', name: 'logistics', component: () => import('./pages/LogisticsPage.vue'), meta: { workspace: 'logistics' } },
          { path: 'workbench', name: 'workbench', component: WorkbenchPage, meta: { workspace: 'operations' } },
          { path: 'knowledge', name: 'knowledge', component: KnowledgePage, meta: { workspace: 'operations' } },
          { path: 'agent-evaluations', name: 'agent-evaluations', component: AgentEvaluationsPage, meta: { workspace: 'operations' } },
          { path: 'audit-events', name: 'audit-events', component: AuditEventsPage, meta: { workspace: 'operations' } },
          { path: 'admin', name: 'admin', component: SystemManagementPage, meta: { workspace: 'shared' } },
          { path: 'analysis', name: 'analysis', component: AnalysisPage, meta: { workspace: 'operations' } },
          { path: 'analysis/:runId', name: 'analysis-run', component: AnalysisRunPage, meta: { workspace: 'operations' } },
          { path: 'proposals', name: 'proposals', component: WorkbenchPage, meta: { workspace: 'operations' } },
          { path: 'proposals/:proposalId', name: 'proposal', component: ProposalPage, meta: { workspace: 'operations' } },
          {
            path: 'approvals',
            name: 'approvals',
            component: ApprovalsPage,
            meta: { approvalOnly: true, workspace: 'operations' },
          },
        ],
      },
      { path: '/:pathMatch(.*)*', redirect: () => session.user ? homeLocation(session.user) : { name: 'workbench' } },
    ],
  })

  router.beforeEach((to) => {
    if (to.name === 'login' && session.user) {
      return to.query.next === 'logistics' && canAccessDepartment(session.user.role, session.user.department, 'logistics')
        ? logisticsLocation(to.query.view)
        : homeLocation(session.user)
    }
    if (to.meta.requiresAuth && !session.user) return { name: 'login', query: to.name === 'logistics' ? { next: 'logistics', ...logisticsLocation(to.query.view).query } : {} }
    if (session.user && (to.meta.workspace === 'operations' || to.meta.workspace === 'logistics') && !canAccessDepartment(session.user.role, session.user.department, to.meta.workspace)) {
      return { name: 'forbidden' }
    }
    const mobile = typeof matchMedia === 'function' && matchMedia('(max-width: 767px)').matches
    if (to.name === 'knowledge' && session.user && !canUseKnowledge(session.user.role, session.user.department, mobile)) {
      return { name: canUseKnowledge(session.user.role, session.user.department, false) ? 'desktop-required' : 'forbidden', query: to.query.workspace === 'logistics' ? { workspace: 'logistics' } : {} }
    }
    if (to.name === 'agent-evaluations' && session.user && !canViewAgentObservability(session.user.role, session.user.department, mobile)) {
      return { name: canViewAgentObservability(session.user.role, session.user.department, false) ? 'desktop-required' : 'forbidden', query: to.query.workspace === 'logistics' ? { workspace: 'logistics' } : {} }
    }
    if (to.name === 'audit-events' && session.user && !canViewAuditEvents(session.user.role, session.user.department, mobile)) {
      return { name: canViewAuditEvents(session.user.role, session.user.department, false) ? 'desktop-required' : 'forbidden', query: to.query.workspace === 'logistics' ? { workspace: 'logistics' } : {} }
    }
    if (to.name === 'admin' && session.user && !canManageSystem(session.user.role, mobile)) {
      return { name: canManageSystem(session.user.role, false) ? 'desktop-required' : 'forbidden', query: to.query.workspace === 'logistics' ? { workspace: 'logistics' } : {} }
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
