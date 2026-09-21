<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { ApiError, listStores, searchKnowledge, listKnowledgeDocuments, getKnowledgeVersions, uploadKnowledgeDocument, disableKnowledgeDocument } from '../api'
import { canUseKnowledge } from '../capabilities'
import { session } from '../session'
import type { KnowledgeDocument, KnowledgeSearchResult, KnowledgeVersionHistory, StoreSummary } from '../types'

const isMobile = typeof matchMedia === 'function' && matchMedia('(max-width: 767px)').matches
const allowed = computed(() => !!session.user && canUseKnowledge(session.user.role, session.user.department, isMobile))
const admin = computed(() => session.user?.role === 'admin')
const stores = ref<StoreSummary[]>([]), storeId = ref(''), query = ref('')
const result = ref<KnowledgeSearchResult | null>(null), searching = ref(false)
const documents = ref<KnowledgeDocument[]>([]), page = ref(1), total = ref(0), loading = ref(false)
const category = ref(''), enabled = ref(''), versionStatus = ref('')
const history = ref<KnowledgeVersionHistory | null>(null), selected = ref(''), historyPage = ref(1), drawer = ref(false)
const name = ref(''), uploadCategory = ref(''), file = ref<File | null>(null), pending = ref(false)
const error = ref(''), notice = ref('')
const lifetime = new AbortController()
let searchController: AbortController | undefined, documentController: AbortController | undefined, historyController: AbortController | undefined
const statuses = ['accepted','processing','active','failed','disabled']

function failure(caught: unknown): void {
  if (lifetime.signal.aborted || caught instanceof DOMException && caught.name === 'AbortError') return
  const messages: Record<number,string> = {401:'登录已失效，请重新登录',403:'没有操作权限',404:'记录不存在或不可访问',409:'状态冲突，请刷新后重试',422:'输入无效，请检查筛选或表单'}
  error.value = caught instanceof ApiError ? (caught.code === 'KNOWLEDGE_DEPENDENCY_TIMEOUT' ? '检索超时，请重试' : messages[caught.status] ?? '服务暂不可用，请重试') : '请求失败，请重试'
}
async function loadDocuments(): Promise<void> {
  if (!allowed.value || !admin.value) return
  documentController?.abort(); const controller = documentController = new AbortController()
  loading.value = true; error.value = ''; documents.value = []
  try {
    const response = await listKnowledgeDocuments({page:page.value, ...(category.value ? {category:category.value} : {}),
      ...(enabled.value ? {enabled:enabled.value === 'true'} : {}), ...(versionStatus.value ? {version_status:versionStatus.value} : {})},controller.signal)
    if (!controller.signal.aborted) { documents.value = response.data.items; total.value = response.data.total }
  } catch (caught) { failure(caught) } finally { if (!controller.signal.aborted) loading.value = false }
}
async function search(): Promise<void> {
  if (!allowed.value || !storeId.value || !query.value.trim()) return
  searchController?.abort(); const controller = searchController = new AbortController()
  searching.value = true; error.value = ''; result.value = null
  try { const response = await searchKnowledge({store_id:storeId.value,query:query.value},controller.signal); if (!controller.signal.aborted) result.value = response }
  catch (caught) { failure(caught) } finally { if (!controller.signal.aborted) searching.value = false }
}
async function openHistory(id: string, nextPage = 1): Promise<void> {
  if (!allowed.value || !admin.value) return
  historyController?.abort(); const controller = historyController = new AbortController()
  selected.value = id; historyPage.value = nextPage; history.value = null; drawer.value = true; error.value = ''
  try { const response = await getKnowledgeVersions(id,nextPage,controller.signal); if (!controller.signal.aborted) history.value = response }
  catch (caught) { failure(caught) }
}
function chooseFile(event: Event): void { file.value = (event.target as HTMLInputElement).files?.[0] ?? null }
async function upload(documentId?: string): Promise<void> {
  if (!allowed.value || !admin.value || pending.value || !file.value) return
  pending.value = true; error.value = ''; notice.value = ''
  const body = new FormData(); body.set('file',file.value)
  if (!documentId) { body.set('name',name.value); body.set('category',uploadCategory.value) }
  try {
    await uploadKnowledgeDocument(body,documentId,lifetime.signal); notice.value = '已接受，等待索引处理'; file.value = null
    await loadDocuments(); if (documentId) await openHistory(documentId)
  } catch (caught) { failure(caught) } finally { pending.value = false }
}
async function disable(document: KnowledgeDocument): Promise<void> {
  if (!allowed.value || !admin.value || pending.value) return
  try { await ElMessageBox.confirm(`停用「${document.name}」后将停止检索该文档。`, '确认停用', {confirmButtonText:'停用',cancelButtonText:'取消',type:'warning'}) } catch { return }
  pending.value = true; error.value = ''
  try { await disableKnowledgeDocument(document.document_id,lifetime.signal); await loadDocuments(); notice.value = '文档已停用' }
  catch (caught) { failure(caught) } finally { pending.value = false }
}
onMounted(async () => {
  if (!allowed.value) return
  try { stores.value = await listStores(lifetime.signal) } catch (caught) { failure(caught); return }
  if (admin.value) await loadDocuments()
})
onBeforeUnmount(() => { lifetime.abort(); searchController?.abort(); documentController?.abort(); historyController?.abort() })
</script>

<template>
  <section aria-labelledby="knowledge-title">
    <header class="page-heading"><p class="eyebrow">KNOWLEDGE</p><h1 id="knowledge-title">知识库</h1><p class="muted">在授权店铺中检索知识，查看可追溯的版本与引用。</p></header>
    <p v-if="isMobile">请使用桌面或平板访问知识库。</p>
    <p v-else-if="!allowed && !error">没有访问权限</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p aria-live="polite">{{ notice }}</p>
    <template v-if="allowed">
      <form class="controls" @submit.prevent="search">
        <label>店铺 <select v-model="storeId" aria-label="检索店铺"><option value="">选择店铺</option><option v-for="store in stores" :key="store.id" :value="store.id">{{ store.name }}</option></select></label>
        <label>检索内容 <input v-model="query" aria-label="检索内容" maxlength="500" required></label>
        <el-button native-type="submit" type="primary" :loading="searching" :disabled="!storeId || !query.trim()">检索</el-button>
      </form>
      <p v-if="searching" aria-live="polite">正在检索…</p>
      <p v-else-if="result?.quality?.status === 'zero_hit'" role="status">未检索到相关知识</p>
      <p v-else-if="result?.quality?.status === 'low_confidence'" role="status">结果置信度较低，请核对引用</p>
      <article v-for="hit in result?.data.citations ?? []" :key="hit.chunk_id" class="citation">
        <strong>{{ hit.document_name }} · 版本 {{ hit.version_number }}</strong><p>{{ hit.canonical_text }}</p>
        <small>{{ hit.category }} · {{ hit.chunk_id }} · 得分 {{ hit.final_score }}</small>
      </article>
      <section v-if="admin" aria-label="文档管理">
        <h2>文档管理</h2>
        <form class="controls" @submit.prevent="page = 1; loadDocuments()">
          <label>类别 <input v-model="category" maxlength="64" aria-label="文档类别筛选"></label>
          <label>启用状态 <select v-model="enabled"><option value="">全部</option><option value="true">启用</option><option value="false">停用</option></select></label>
          <label>版本状态 <select v-model="versionStatus"><option value="">全部</option><option v-for="s in statuses" :key="s">{{ s }}</option></select></label>
          <el-button native-type="submit" :disabled="loading">筛选</el-button>
        </form>
        <el-skeleton v-if="loading" :rows="4" animated />
        <el-empty v-else-if="!documents.length && !error" :description="category || enabled || versionStatus ? '没有符合筛选条件的文档' : '暂无文档'" />
        <el-table v-else :data="documents">
          <el-table-column prop="name" label="文档" /><el-table-column prop="category" label="类别" />
          <el-table-column label="状态"><template #default="{row}">{{ row.enabled ? '启用' : '停用' }} · {{ row.current_version_status ?? '等待索引' }}</template></el-table-column>
          <el-table-column label="操作"><template #default="{row}"><el-button text @click="openHistory(row.document_id)">版本历史</el-button><el-button text :disabled="!row.enabled || pending" @click="disable(row)">停用</el-button></template></el-table-column>
        </el-table>
        <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="loadDocuments" />
        <h3>上传文档</h3>
        <form class="controls" @submit.prevent="upload()">
          <label>文档名称 <input v-model="name" required maxlength="128"></label><label>类别 <input v-model="uploadCategory" required maxlength="64"></label>
          <label>文件 <input type="file" accept=".md,.txt,.pdf,.docx" @change="chooseFile"></label>
          <el-button native-type="submit" :loading="pending" :disabled="!file || !name || !uploadCategory">上传文档</el-button>
        </form>
        <el-drawer v-model="drawer" title="版本历史" size="65%">
          <template v-if="history"><h2>{{ history.name }}</h2>
            <el-table :data="history.items"><el-table-column prop="version_number" label="版本" /><el-table-column prop="status" label="状态" /><el-table-column prop="parser_version" label="解析器" /><el-table-column prop="chunker_version" label="分块" /><el-table-column prop="embedding_version" label="嵌入" /><el-table-column prop="error_code" label="错误码" /><el-table-column prop="created_at" label="时间" /></el-table>
            <el-pagination v-model:current-page="historyPage" :page-size="20" :total="history.total" layout="prev, pager, next" @current-change="openHistory(selected,historyPage)" />
            <form v-if="history.enabled" class="controls" @submit.prevent="upload(selected)"><label>新版本文件 <input type="file" accept=".md,.txt,.pdf,.docx" @change="chooseFile"></label><el-button native-type="submit" :loading="pending" :disabled="!file">创建版本</el-button></form>
          </template><p v-else-if="error" role="alert">{{ error }}</p><el-skeleton v-else :rows="4" animated />
        </el-drawer>
      </section>
    </template>
  </section>
</template>
<style scoped>
.controls { display:flex; flex-wrap:wrap; align-items:end; gap:16px; margin:20px 0; }
label { display:grid; gap:6px; }
input, select { padding:9px; border:1px solid #cbd5df; border-radius:4px; background:var(--app-surface); color:inherit; }
.citation { padding:18px 0; border-bottom:1px solid #dbe2e8; }
section section { margin-top:40px; }
</style>
