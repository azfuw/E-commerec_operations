<script setup lang="ts">
import {computed,onMounted,onBeforeUnmount,ref} from 'vue'
import {ApiError,listAgentEvaluationRuns,getAgentEvaluationRun,listAgentCalls} from '../api'
import {canViewAgentObservability} from '../capabilities'
import {session} from '../session'
import type {EvaluationRun,EvaluationRunDetail,EvaluationAgentType,AgentCall} from '../types'
const mobile = typeof matchMedia === 'function' && matchMedia('(max-width: 767px)').matches
const allowed = computed(()=>!!session.user && canViewAgentObservability(session.user.role,session.user.department,mobile))
const tab=ref('runs'),page=ref(1),total=ref(0),loading=ref(false),error=ref('')
const storeId=ref(''),agent=ref<EvaluationAgentType|''>(''),status=ref<'completed'|'failed'|''>('')
const workflow=ref(''),node=ref(''),callStatus=ref<'succeeded'|'failed'|''>(''),code=ref('')
const runs=ref<EvaluationRun[]>([]),calls=ref<AgentCall[]>([]),detail=ref<EvaluationRunDetail|null>(null),drawer=ref(false)
let controller:AbortController|undefined,detailController:AbortController|undefined
const nodes=['call_analysis_agent','validate_and_reconcile','call_product_optimization_agent','repair_product_optimization_schema','call_product_compliance_agent','repair_product_compliance_schema']
const codes=['DEEPSEEK_KEY_MISSING','LEASE_LOST','DEEPSEEK_TIMEOUT','DEEPSEEK_TRANSPORT','DEEPSEEK_RATE_LIMIT','DEEPSEEK_SERVER_ERROR','DEEPSEEK_UNAUTHORIZED','DEEPSEEK_FORBIDDEN','DEEPSEEK_SCHEMA_INVALID','DEEPSEEK_HTTP_ERROR']
function failure(caught:unknown):void{
  const labels:Record<number,string>={401:'登录已失效，请重新登录',403:'没有访问权限',404:'记录不存在或不可访问',409:'状态冲突，请刷新后重试',422:'筛选条件无效'}
  error.value=caught instanceof ApiError ? labels[caught.status]??'服务暂不可用，请重试' : '请求失败，请重试'
}
async function load():Promise<void>{
  if(!allowed.value)return
  controller?.abort();const current=controller=new AbortController();loading.value=true;error.value='';runs.value=[];calls.value=[]
  const common={page:page.value,page_size:20,...(storeId.value?{store_id:storeId.value}:{})}
  try{
    if(tab.value==='runs'){
      const response=await listAgentEvaluationRuns({...common,...(agent.value?{agent_type:agent.value}:{}),...(status.value?{status:status.value}:{})},current.signal)
      if(!current.signal.aborted){runs.value=response.items;total.value=response.total}
    }else{
      const response=await listAgentCalls({...common,...(workflow.value?{workflow_type:workflow.value}:{}),...(node.value?{node_name:node.value}:{}),...(callStatus.value?{status:callStatus.value}:{}),...(code.value?{error_code:code.value}:{})},current.signal)
      if(!current.signal.aborted){calls.value=response.items;total.value=response.total}
    }
  }catch(caught){if(!current.signal.aborted)failure(caught)}finally{if(!current.signal.aborted)loading.value=false}
}
async function open(id:string):Promise<void>{
  if(!allowed.value)return
  detailController?.abort();const current=detailController=new AbortController();drawer.value=true;detail.value=null;error.value=''
  try{const response=await getAgentEvaluationRun(id,current.signal);if(!current.signal.aborted)detail.value=response}
  catch(caught){if(!current.signal.aborted)failure(caught)}
}
onMounted(()=>void load());onBeforeUnmount(()=>{controller?.abort();detailController?.abort()})
</script>
<template>
  <section aria-labelledby="evaluation-title">
    <header class="page-heading"><p class="eyebrow">AGENT OBSERVABILITY</p><h1 id="evaluation-title">Agent 评测</h1><p class="muted">固定离线评测证据与工作流调用摘要。</p></header>
    <p v-if="mobile">请使用桌面或平板访问评测模块。</p><p v-else-if="!allowed && !error">没有访问权限</p>
    <p v-if="error" role="alert" class="inline-error">{{error}}</p>
    <template v-if="allowed">
      <el-tabs v-model="tab" @tab-change="page=1;load()"><el-tab-pane name="runs" label="离线评测"/><el-tab-pane name="calls" label="Agent 调用"/></el-tabs>
      <form class="filters" @submit.prevent="page=1;load()">
        <label>店铺 ID <input v-model="storeId" maxlength="36" placeholder="全部可见店铺"></label>
        <template v-if="tab==='runs'"><label>Agent 类型 <select v-model="agent"><option value="">全部</option><option v-for="value in ['analysis','optimization','compliance','knowledge_retrieval']" :key="value">{{value}}</option></select></label><label>运行状态 <select v-model="status"><option value="">全部</option><option value="completed">已完成</option><option value="failed">失败</option></select></label></template>
        <template v-else><label>工作流 <select v-model="workflow"><option value="">全部</option><option v-for="value in ['analysis','optimization','manual_review']" :key="value">{{value}}</option></select></label><label>节点 <select v-model="node"><option value="">全部</option><option v-for="value in nodes" :key="value">{{value}}</option></select></label><label>调用状态 <select v-model="callStatus"><option value="">全部</option><option value="succeeded">成功</option><option value="failed">失败</option></select></label><label>错误码 <select v-model="code"><option value="">全部</option><option v-for="value in codes" :key="value">{{value}}</option></select></label></template>
        <el-button native-type="submit" :disabled="loading">筛选</el-button>
      </form>
      <p aria-live="polite">{{loading?'正在加载…':`共 ${total} 项`}}</p><el-skeleton v-if="loading" :rows="5" animated/>
      <el-empty v-else-if="!runs.length && !calls.length && !error" :description="storeId||(tab==='runs' ? agent||status : workflow||node||callStatus||code)?'没有符合筛选条件的记录':tab==='runs'?'暂无评测记录':'暂无调用记录'"/>
      <el-table v-else-if="tab==='runs'" :data="runs"><el-table-column min-width="140" prop="agent_type" label="Agent"/><el-table-column min-width="140" prop="store_id" label="店铺（空为全局）"/><el-table-column min-width="140" prop="status" label="状态"/><el-table-column min-width="140" label="通过 / 总数"><template #default="{row}">{{row.summary.passed_cases}} / {{row.summary.total_cases}}</template></el-table-column><el-table-column min-width="140" prop="created_at" label="时间"/><el-table-column min-width="140" label="证据"><template #default="{row}"><el-button text @click="open(row.id)">查看结果</el-button></template></el-table-column></el-table>
      <el-table v-else :data="calls"><el-table-column min-width="140" prop="workflow_run_id" label="工作流 ID"/><el-table-column min-width="140" prop="store_id" label="店铺"/><el-table-column min-width="140" prop="node_name" label="节点"/><el-table-column min-width="140" prop="call_type" label="调用类型"/><el-table-column min-width="140" prop="iteration" label="轮次"/><el-table-column min-width="140" prop="attempt" label="尝试"/><el-table-column min-width="140" prop="model" label="模型"/><el-table-column min-width="140" prop="prompt_version" label="提示词版本"/><el-table-column min-width="140" prop="status" label="状态"/><el-table-column min-width="140" prop="total_tokens" label="总 Token"/><el-table-column min-width="140" prop="duration_ms" label="耗时 ms"/><el-table-column min-width="140" prop="estimated_cost" label="估算费用"/><el-table-column min-width="140" prop="error_code" label="错误码"/></el-table>
      <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load"/>
      <el-drawer v-model="drawer" title="离线评测结果" size="70%"><template v-if="detail"><p>{{detail.agent_type}} · {{detail.status}} · {{detail.suite_version}} / {{detail.runner_version}} / {{detail.dataset_version}}</p><p>开始 {{detail.started_at}} · 完成 {{detail.completed_at}}</p><p>{{detail.error_code}}</p><dl><template v-for="(value,key) in detail.summary" :key="key"><dt>{{key}}</dt><dd>{{value}}</dd></template></dl><el-table :data="detail.results"><el-table-column min-width="140" prop="case_key" label="用例"/><el-table-column min-width="140" prop="case_version" label="版本"/><el-table-column min-width="140" prop="outcome" label="结果"/><el-table-column min-width="140" prop="result_code" label="结果码"/><el-table-column min-width="140" prop="latency_ms" label="耗时 ms"/><el-table-column min-width="140" label="指标"><template #default="{row}"><p v-for="(value,key) in row.metrics" :key="key">{{key}}: {{value}}</p></template></el-table-column></el-table></template><p v-else-if="error" role="alert">{{error}}</p><el-skeleton v-else :rows="4" animated/></el-drawer>
    </template>
  </section>
</template>
<style scoped>.filters{display:flex;flex-wrap:wrap;align-items:end;gap:16px;margin:20px 0}label{display:grid;gap:6px}input,select{padding:9px;border:1px solid #cbd5df;border-radius:4px;background:var(--app-surface);color:inherit}dt{color:var(--app-muted)}dd{margin:0 0 8px}</style>
