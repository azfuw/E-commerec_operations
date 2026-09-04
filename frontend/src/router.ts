import { createRouter, createWebHistory } from 'vue-router'
import { h } from 'vue'
import { canUseKnowledge } from './capabilities'
import KnowledgePage from './pages/KnowledgePage.vue'

import AppShell from './components/AppShell.vue'
import ForbiddenPage from './pages/ForbiddenPage.vue'
import LoginPage from './pages/LoginPage.vue'
import AnalysisPage from './pages/AnalysisPage.vue'
import AnalysisRunPage from './pages/AnalysisRunPage.vue'
import ProposalPage from './pages/ProposalPage.vue'
import ApprovalsPage from './pages/ApprovalsPage.vue'
import WorkbenchPage from './pages/WorkbenchPage.vue'
import { session } from './session'

export function createAppRouter() {
  const router = createRouter({
    history: createWebHistory('/app/'),
    routes: [
      { path: '/login', name: 'login', component: LoginPage },
      { path: '/forbidden', name: 'forbidden', component: ForbiddenPage },
      { path: '/desktop-required', name: 'desktop-required', component: { render: () => h('section', {class:'page-state'}, [h('h1','请使用桌面或平板访问管理模块'), h('a',{href:'/app/workbench'},'返回工作台')]) } },
      {
        path: '/',
        component: AppShell,
        meta: { requiresAuth: true },
        children: [
          { path: '', redirect: { name: 'workbench' } },
          { path: 'workbench', name: 'workbench', component: WorkbenchPage },
          { path: 'knowledge', name: 'knowledge', component: KnowledgePage },
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
    if (to.name === 'login' && session.user) return { name: 'workbench' }
    if (to.meta.requiresAuth && !session.user) return { name: 'login' }
    const mobile = typeof matchMedia === 'function' && matchMedia('(max-width: 767px)').matches
    if (to.name === 'knowledge' && session.user && !canUseKnowledge(session.user.role, mobile)) {
      return { name: mobile ? 'desktop-required' : 'forbidden' }
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
