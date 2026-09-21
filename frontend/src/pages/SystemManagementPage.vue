<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { ApiError, listAdminUsers, listAdminStores, updateAdminUser, replaceAdminUserScopes, updateAdminStore } from '../api'
import { canManageSystem } from '../capabilities'
import { session } from '../session'
import type { AdminUser, AdminStore, UserDepartment, UserRole, UserStatus } from '../types'

const mobile = typeof matchMedia === 'function' && matchMedia('(max-width: 767px)').matches
const allowed = computed(() => !!session.user && canManageSystem(session.user.role, mobile))
const tab = ref('users'), page = ref(1), total = ref(0)
const users = ref<AdminUser[]>([]), stores = ref<AdminStore[]>([])
const filters = reactive({ role: '' as UserRole | '', status: '' as UserStatus | '', store_id: '', enabled: '' })
const loading = ref(false), pending = ref(false), error = ref(''), notice = ref('')
const selected = ref<AdminUser | null>(null), editor = ref('')
const form = reactive({ role: 'operator' as UserRole, department: 'operations' as UserDepartment, status: 'active' as UserStatus, scopes: '' })
const lifetime = new AbortController()
let controller: AbortController | undefined
const roles: UserRole[] = ['operator', 'supervisor', 'admin']
const roleNames = { operator: '员工', supervisor: '主管', admin: '管理员' }
const departmentNames = { operations: '运营', logistics: '物流' }
const filtered = computed(() => tab.value === 'users' ? !!(filters.role || filters.status || filters.store_id) : !!filters.enabled)

function failure(caught: unknown): void {
  if (lifetime.signal.aborted) return
  const messages: Record<number, string> = { 401: '登录已失效，请重新登录', 403: '没有操作权限', 404: '用户或店铺不存在，请刷新', 409: '状态冲突或受保护操作，请刷新后重试', 422: '输入无效，请检查表单或筛选条件' }
  error.value = caught instanceof ApiError ? messages[caught.status] ?? '服务暂不可用，请重试' : '请求失败，请重试'
}
async function load(): Promise<void> {
  if (!allowed.value) return
  controller?.abort(); const current = controller = new AbortController()
  loading.value = true; error.value = ''; users.value = []; stores.value = []
  try {
    if (tab.value === 'users') {
      const result = await listAdminUsers({ page: page.value, page_size: 20,
        ...(filters.role ? { role: filters.role } : {}), ...(filters.status ? { status: filters.status } : {}),
        ...(filters.store_id ? { store_id: filters.store_id } : {}) }, current.signal)
      if (!current.signal.aborted) { users.value = result.items; total.value = result.total }
    } else {
      const result = await listAdminStores({ page: page.value, page_size: 20,
        ...(filters.enabled ? { enabled: filters.enabled === 'true' } : {}) }, current.signal)
      if (!current.signal.aborted) { stores.value = result.items; total.value = result.total }
    }
  } catch (caught) { if (!current.signal.aborted) failure(caught) }
  finally { if (!current.signal.aborted) loading.value = false }
}
function edit(user: AdminUser, mode: string): void {
  selected.value = user; editor.value = mode; error.value = ''; notice.value = ''
  form.role = user.role; form.department = user.department; form.status = user.status; form.scopes = user.store_ids.join('\n')
}
async function confirm(message: string): Promise<boolean> {
  try { await ElMessageBox.confirm(message, '确认变更', { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' }); return true }
  catch { return false }
}
async function save(): Promise<void> {
  if (!allowed.value || pending.value || !selected.value) return
  const user = selected.value
  pending.value = true; error.value = ''; notice.value = ''
  try {
    if (editor.value === 'user') {
      if (user.id === session.user?.id && (form.role !== 'admin' || form.status !== 'active')) return
      if (form.status !== user.status && !await confirm(`将「${user.username}」设为${form.status === 'disabled' ? '停用' : '启用'}？`)) return
      await updateAdminUser(user.id, { role: form.role, department: form.department, status: form.status }, lifetime.signal)
    } else {
      const ids = form.scopes.split(/\s+/).filter(Boolean)
      if (ids.length > 100 || new Set(ids).size !== ids.length || ids.some(id => id.length > 36)) {
        error.value = '店铺 ID 不可重复，每项最多 36 字符，最多 100 项'; return
      }
      await replaceAdminUserScopes(user.id, ids, lifetime.signal)
    }
    editor.value = ''; notice.value = '变更已保存'; await load()
  } catch (caught) { failure(caught) }
  finally { pending.value = false }
}
async function toggleStore(store: AdminStore): Promise<void> {
  if (!allowed.value || pending.value) return
  pending.value = true; error.value = ''; notice.value = ''
  try {
    if (!await confirm(`${store.enabled ? '停用' : '启用'}「${store.name}」？历史记录将保留。`)) return
    await updateAdminStore(store.id, !store.enabled, lifetime.signal)
    notice.value = '店铺状态已更新'; await load()
  } catch (caught) { failure(caught) }
  finally { pending.value = false }
}
onMounted(() => void load())
onBeforeUnmount(() => { lifetime.abort(); controller?.abort() })
</script>

<template>
  <section aria-labelledby="admin-title">
    <header class="page-heading"><p class="eyebrow">SYSTEM MANAGEMENT</p><h1 id="admin-title">系统管理</h1><p class="muted">管理现有用户、店铺权限与启停状态。</p></header>
    <p v-if="session.user?.role !== 'admin' && !error">没有访问权限</p>
    <p v-else-if="mobile">请使用桌面或平板访问系统管理。</p>
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <p aria-live="polite">{{ notice }}</p>
    <template v-if="allowed">
      <el-tabs v-model="tab" @tab-change="page = 1; load()">
        <el-tab-pane label="用户与权限" name="users" :disabled="pending" />
        <el-tab-pane label="店铺启停" name="stores" :disabled="pending" />
      </el-tabs>
      <form class="filters" @submit.prevent="page = 1; load()">
        <template v-if="tab === 'users'">
          <label>角色 <select v-model="filters.role" aria-label="用户角色筛选"><option value="">全部</option><option v-for="role in roles" :key="role" :value="role">{{ roleNames[role] }}</option></select></label>
          <label>用户状态 <select v-model="filters.status"><option value="">全部</option><option value="active">启用</option><option value="disabled">停用</option></select></label>
          <label>授权店铺 ID <input v-model="filters.store_id" maxlength="36"></label>
        </template>
        <label v-else>店铺状态 <select v-model="filters.enabled"><option value="">全部</option><option value="true">启用</option><option value="false">停用</option></select></label>
        <el-button native-type="submit" :disabled="loading || pending">筛选 / 刷新</el-button>
      </form>
      <p aria-live="polite">{{ loading ? '正在加载…' : `共 ${total} 项` }}</p>
      <el-skeleton v-if="loading" :rows="5" animated />
      <template v-else-if="!error">
        <template v-if="tab === 'users'">
          <el-empty v-if="!users.length" :description="filtered ? '没有符合筛选条件的用户' : '暂无用户'" />
          <el-table v-else :data="users">
            <el-table-column prop="username" label="用户名" min-width="120" />
            <el-table-column label="部门" width="90"><template #default="{row}">{{ departmentNames[row.department as UserDepartment] }}</template></el-table-column>
            <el-table-column label="角色" width="100"><template #default="{row}">{{ roleNames[row.role as UserRole] }}</template></el-table-column>
            <el-table-column label="状态" width="90"><template #default="{row}"><el-tag :type="row.status === 'active' ? 'success' : 'info'">{{ row.status === 'active' ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="授权店铺" min-width="140"><template #default="{row}">{{ row.store_ids.join('、') || '无' }}</template></el-table-column>
            <el-table-column label="操作" width="210"><template #default="{row}"><el-button text :disabled="pending" @click="edit(row, 'user')">编辑用户</el-button><el-button text :disabled="pending" @click="edit(row, 'scopes')">店铺权限</el-button></template></el-table-column>
          </el-table>
        </template>
        <template v-else>
          <el-empty v-if="!stores.length" :description="filtered ? '没有符合筛选条件的店铺' : '暂无店铺'" />
          <el-table v-else :data="stores">
            <el-table-column prop="name" label="店铺" min-width="120" /><el-table-column prop="id" label="店铺 ID" min-width="140" /><el-table-column prop="code" label="编码" min-width="100" />
            <el-table-column label="状态" width="90"><template #default="{row}"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="100"><template #default="{row}"><el-button text :disabled="pending" @click="toggleStore(row)">{{ row.enabled ? '停用店铺' : '启用店铺' }}</el-button></template></el-table-column>
          </el-table>
        </template>
        <el-pagination v-model:current-page="page" :page-size="20" :total="total" :disabled="pending" layout="prev, pager, next" @current-change="load" />
      </template>
      <el-drawer :model-value="!!editor" :title="editor === 'user' ? '编辑用户' : '店铺权限'" size="min(560px, 85vw)" :show-close="!pending" :close-on-click-modal="!pending" :close-on-press-escape="!pending" @close="editor = ''">
        <template v-if="selected">
          <p>{{ selected.username }} · {{ selected.id }}</p><p class="muted">创建于 {{ selected.created_at }}</p>
          <form class="edit-form" @submit.prevent="save">
            <template v-if="editor === 'user'">
              <label>用户角色 <select v-model="form.role" aria-label="用户角色" :disabled="pending || selected.id === session.user?.id"><option v-for="role in roles" :key="role" :value="role">{{ roleNames[role] }}</option></select></label>
              <label>所属部门 <select v-model="form.department" aria-label="所属部门" :disabled="pending"><option value="operations">运营</option><option value="logistics">物流</option></select></label>
              <label>用户状态 <select v-model="form.status" aria-label="用户状态" :disabled="pending || selected.id === session.user?.id"><option value="active">启用</option><option value="disabled">停用</option></select></label>
              <p v-if="selected.id === session.user?.id" class="muted">不能停用自己或降低自己的管理员角色。</p>
            </template>
            <template v-else>
              <label>授权店铺 ID（每行一个）<textarea v-model="form.scopes" rows="8" maxlength="3700" :disabled="pending" aria-describedby="scope-help" /></label>
              <p id="scope-help" class="muted">可在“店铺启停”查看 ID。保存会替换完整权限集合；留空将清除全部店铺权限。仅可授权启用的店铺。</p>
            </template>
            <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
            <el-button native-type="submit" type="primary" :loading="pending" :disabled="pending">保存变更</el-button>
          </form>
        </template>
      </el-drawer>
    </template>
  </section>
</template>
<style scoped>
.filters{display:flex;flex-wrap:wrap;align-items:end;gap:16px;margin:20px 0}
label{display:grid;gap:6px}input,select,textarea{padding:9px;border:1px solid #cbd5df;border-radius:4px;background:var(--app-surface);color:inherit;font:inherit}
.edit-form{display:grid;gap:18px}.edit-form .el-button{justify-self:start}
</style>
