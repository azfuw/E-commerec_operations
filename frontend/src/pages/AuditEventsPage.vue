<script setup lang="ts">
import {computed,onMounted,onBeforeUnmount,reactive,ref} from 'vue'
import {ApiError,listAuditEvents} from '../api'
import {canViewAuditEvents} from '../capabilities'
import {session} from '../session'
import type {AuditEvent,AuditEventQuery} from '../types'
const mobile=typeof matchMedia==='function'&&matchMedia('(max-width: 767px)').matches
const allowed=computed(()=>!!session.user&&canViewAuditEvents(session.user.role,mobile))
const items=ref<AuditEvent[]>([]),selected=ref<AuditEvent|null>(null),drawer=ref(false),loading=ref(false),error=ref(''),page=ref(1),total=ref(0)
const filters=reactive({store_id:'',proposal_id:'',workflow_run_id:'',actor_id:'',event_type:'',action:'',outcome:''})
const from=ref(''),to=ref('')
const events=['manual_revision_created','manual_review_claimed','manual_review_completed','manual_review_failed','proposal_submitted','proposal_approved','proposal_rejected','proposal_changes_requested','simulated_publish_completed','authorization_denied','knowledge_document_created','knowledge_version_created','knowledge_document_disabled','evaluation_run_persisted','admin_user_updated','admin_user_scopes_replaced','admin_store_updated']
let controller:AbortController|undefined
async function load():Promise<void>{
  if(!allowed.value)return
  if(Boolean(from.value)!==Boolean(to.value)){error.value='请同时选择起止时间（最长 31 天）';return}
  controller?.abort();const current=controller=new AbortController();error.value='';loading.value=true;items.value=[]
  try{
    const query:AuditEventQuery={...filters,page:page.value,page_size:20,...(from.value&&to.value?{created_from:new Date(from.value).toISOString(),created_to:new Date(to.value).toISOString()}: {})}
    const response=await listAuditEvents(query,current.signal)
    if(!current.signal.aborted){items.value=response.items;total.value=response.total}
  }catch(caught){if(!current.signal.aborted){const messages:Record<number,string>={401:'登录已失效，请重新登录',403:'没有访问权限',404:'记录不存在或不可访问',409:'状态冲突，请刷新后重试',422:'筛选无效，请检查起止时间与 31 天范围'};error.value=caught instanceof ApiError?messages[caught.status]??'审计加载失败，请重试':'审计加载失败，请重试'}}
  finally{if(!current.signal.aborted)loading.value=false}
}
onMounted(()=>void load());onBeforeUnmount(()=>controller?.abort())
</script>
<template>
  <section aria-labelledby="audit-title"><header class="page-heading"><p class="eyebrow">AUDIT LOG</p><h1 id="audit-title">审计日志</h1><p class="muted">按授权范围查看操作记录与安全详情。</p></header>
    <p v-if="mobile">请使用桌面或平板访问审计日志。</p><p v-else-if="!allowed&&!error">没有访问权限</p><p v-if="error" role="alert" class="inline-error">{{error}}</p>
    <template v-if="allowed">
      <form class="filters" @submit.prevent="page=1;load()">
        <label>店铺 ID <input v-model="filters.store_id" aria-label="审计店铺 ID" maxlength="36"></label>
        <label>方案 ID <input v-model="filters.proposal_id" maxlength="36"></label><label>工作流 ID <input v-model="filters.workflow_run_id" maxlength="36"></label><label>操作人 ID <input v-model="filters.actor_id" maxlength="36"></label>
        <label>事件 <select v-model="filters.event_type"><option value="">全部</option><option v-for="event in events" :key="event">{{event}}</option></select></label>
        <label>审批动作 <select v-model="filters.action"><option value="">全部</option><option v-for="action in ['submit','approve','reject','request_changes']" :key="action">{{action}}</option></select></label>
        <label>结果 <select v-model="filters.outcome"><option value="">全部</option><option value="success">成功</option><option value="failed">失败</option><option value="denied">拒绝</option></select></label>
        <label>开始时间 <input v-model="from" type="datetime-local"></label><label>结束时间 <input v-model="to" type="datetime-local"></label><el-button native-type="submit" :disabled="loading">筛选</el-button>
      </form>
      <p aria-live="polite">{{loading?'正在加载…':`共 ${total} 项`}}</p><el-skeleton v-if="loading" :rows="5" animated/>
      <el-empty v-else-if="!items.length&&!error" :description="Object.values(filters).some(Boolean)||from||to?'没有符合筛选条件的记录':'暂无审计记录'"/>
      <el-table v-else :data="items"><el-table-column min-width="140" prop="created_at" label="时间"/><el-table-column min-width="140" prop="event_type" label="事件"/><el-table-column min-width="140" prop="actor_id" label="操作人"/><el-table-column min-width="140" prop="store_id" label="店铺（空为全局）"/><el-table-column min-width="140" prop="outcome" label="结果"/><el-table-column min-width="140" prop="resource_type" label="资源类型"/><el-table-column min-width="140" prop="error_code" label="错误码"/><el-table-column min-width="140" label="详情"><template #default="{row}"><el-button text @click="selected=row;drawer=true">查看详情</el-button></template></el-table-column></el-table>
      <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load"/>
      <el-drawer v-model="drawer" title="审计详情" size="55%"><template v-if="selected"><p>{{selected.event_type}} · {{selected.outcome}}</p><dl><template v-for="key in (['id','actor_id','actor_role','store_id','proposal_id','proposal_revision_id','workflow_run_id','approval_action_id','publish_record_id','resource_type','resource_id','request_id','created_at','error_code'] as const)" :key="key"><dt>{{key}}</dt><dd>{{selected[key]??'—'}}</dd></template></dl><h3>操作详情</h3><dl><template v-for="(value,key) in selected.details" :key="key"><dt>{{key}}</dt><dd>{{value}}</dd></template></dl></template></el-drawer>
    </template>
  </section>
</template>
<style scoped>
.filters { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); align-items: end; gap: 18px 16px; margin: 24px 0; padding: 22px; border: 1px solid var(--app-border); border-radius: var(--app-radius); background: var(--app-surface); }
.filters label { display: grid; min-width: 0; gap: 8px; color: var(--app-muted); font-size: 12px; }
.filters label:nth-last-of-type(2) { grid-column: 1; }
.filters input, .filters select { width: 100%; min-width: 0; height: 36px; padding: 0 10px; border: 1px solid var(--app-border); border-radius: 5px; background: var(--app-surface); color: var(--app-text); }
.filters .el-button { justify-self: start; min-width: 90px; height: 36px; }
dt { color: var(--app-muted); }
dd { margin: 0 0 10px; overflow-wrap: anywhere; }
:deep(.el-empty) { min-height: 240px; border: 1px solid var(--app-border); border-radius: var(--app-radius); background: var(--app-surface); }
:deep(.el-empty__image) { width: 84px; }
.el-pagination { justify-content: flex-end; margin-top: 18px; }
@media (max-width: 1100px) { .filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
