import { createRouter, createWebHistory } from 'vue-router'

import AppShell from './components/AppShell.vue'
import ForbiddenPage from './pages/ForbiddenPage.vue'
import LoginPage from './pages/LoginPage.vue'
import AnalysisPage from './pages/AnalysisPage.vue'
import AnalysisRunPage from './pages/AnalysisRunPage.vue'
import WorkbenchPage from './pages/WorkbenchPage.vue'
import { session } from './session'

export function createAppRouter() {
  const router = createRouter({
    history: createWebHistory('/app/'),
    routes: [
      { path: '/login', name: 'login', component: LoginPage },
      { path: '/forbidden', name: 'forbidden', component: ForbiddenPage },
      {
        path: '/',
        component: AppShell,
        meta: { requiresAuth: true },
        children: [
          { path: '', redirect: { name: 'workbench' } },
          { path: 'workbench', name: 'workbench', component: WorkbenchPage },
          { path: 'analysis', name: 'analysis', component: AnalysisPage },
          { path: 'analysis/:runId', name: 'analysis-run', component: AnalysisRunPage },
          { path: 'proposals', name: 'proposals', component: WorkbenchPage },
        ],
      },
      { path: '/:pathMatch(.*)*', redirect: { name: 'workbench' } },
    ],
  })

  router.beforeEach((to) => {
    if (to.name === 'login' && session.user) return { name: 'workbench' }
    if (to.meta.requiresAuth && !session.user) return { name: 'login' }
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
