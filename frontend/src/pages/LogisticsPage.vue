<script setup lang="ts">
import {
  ChatLineRound,
  CircleCheck,
  Download,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { listStores } from '../api'
import {
  addShipmentEvent,
  createReturn,
  createShipment,
  exceptionKindLabel,
  exceptionStatusLabel,
  formatShanghai,
  getAgentBrief,
  getLogisticsDashboard,
  getLogisticsView,
  logisticsLocation,
  getShipment,
  listAgentRuns,
  listAssignees,
  listEligibleOrders,
  listExceptions,
  listReturns,
  listShipments,
  overviewEventDescription,
  queryAgent,
  returnStatusLabel,
  runAgentPatrol,
  shanghaiInputValue,
  shipmentStatusLabel,
  toCsv,
  toUtcIso,
  transitionReturn,
  updateException,
  type AgentAnswer,
  type AgentRun,
  type ExceptionKind,
  type ExceptionStatus,
  type LogisticsDashboard,
  type LogisticsException,
  type LogisticsOrder,
  type LogisticsView,
  type ReturnCase,
  type ReturnStatus,
  type Shipment,
  type ShipmentDetail,
  type ShipmentStatus,
} from '../logistics'
import type { StoreSummary } from '../types'

const router = useRouter()
const route = useRoute()
const active = computed(() => getLogisticsView(route.query.view))
const stores = ref<StoreSummary[]>([])
const storeId = ref('')
const dashboard = ref<LogisticsDashboard | null>(null)
const shipments = ref<Shipment[]>([])
const returns = ref<ReturnCase[]>([])
const exceptions = ref<LogisticsException[]>([])
const runs = ref<AgentRun[]>([])
const loading = ref(false)
const actionLoading = ref(false)
const error = ref('')
let loadVersion = 0

const shipmentPage = ref(1)
const shipmentTotal = ref(0)
const shipmentFilters = ref<{ q: string; status: ShipmentStatus | ''; risk_only: boolean }>({ q: '', status: '', risk_only: false })
const returnPage = ref(1)
const returnTotal = ref(0)
const returnFilters = ref<{ q: string; status: ReturnStatus | '' }>({ q: '', status: '' })
const exceptionPage = ref(1)
const exceptionTotal = ref(0)
const exceptionFilters = ref<{ status: ExceptionStatus | ''; kind: ExceptionKind | '' }>({ status: '', kind: '' })
const pageSize = 12

const detailOpen = ref(false)
const detail = ref<ShipmentDetail | null>(null)
const detailLoading = ref(false)
const eventOpen = ref(false)
const eventForm = ref({ action: 'transit' as 'dispatch' | 'transit' | 'deliver', occurred_at: '', location: '', description: '' })
const eventInitialTime = ref('')
const returnOpen = ref(false)
const returnForm = ref({ reason: '', expected_return_at: '' })

const createOpen = ref(false)
const orderQuery = ref('')
const orders = ref<LogisticsOrder[]>([])
const orderLoading = ref(false)
const createForm = ref({ order_id: '', carrier: '', tracking_no: '', destination: '', dispatch_due_at: '', expected_delivery_at: '' })

const returnActionOpen = ref(false)
const selectedReturn = ref<ReturnCase | null>(null)
const returnAction = ref({ status: 'approved' as Exclude<ReturnStatus, 'requested'>, occurred_at: '', carrier: '', tracking_no: '', note: '' })
const returnActionInitialTime = ref('')

const exceptionOpen = ref(false)
const selectedException = ref<LogisticsException | null>(null)
const assignees = ref<{ id: string; username: string }[]>([])
const exceptionAction = ref({ action: 'assign' as 'assign' | 'resolve', assignee_id: '', resolution: '' })

const question = ref('')
const agentBusy = ref(false)
const chat = ref<{ role: 'user' | 'agent'; text: string; answer?: AgentAnswer }[]>([])

const today = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())
const selectedStoreName = computed(() => stores.value.find((store) => store.id === storeId.value)?.name ?? '全部授权店铺')
const activeReturns = computed(() => returns.value.filter((item) => !['closed', 'rejected'].includes(item.status)).length)

const distributionNote: Record<ShipmentStatus, string> = {
  pending_dispatch: '仓库备货与快递交接',
  in_transit: '已发货，关注轨迹与时效',
  delivered: '已完成签收节点',
}

function distributionPercent(count: number): number {
  return dashboard.value?.metrics.total_shipments
    ? Math.round(count / dashboard.value.metrics.total_shipments * 100)
    : 0
}

function fail(message: string): void {
  error.value = message
}

async function loadDashboard(version = ++loadVersion): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const value = await getLogisticsDashboard(storeId.value || undefined)
    if (version === loadVersion) dashboard.value = value
  } catch {
    if (version === loadVersion) fail('物流总览加载失败，请重试')
  } finally {
    if (version === loadVersion) loading.value = false
  }
}

async function loadShipments(version = ++loadVersion): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await listShipments({
      store_id: storeId.value || undefined,
      status: shipmentFilters.value.status || undefined,
      q: shipmentFilters.value.q.trim() || undefined,
      risk_only: shipmentFilters.value.risk_only || undefined,
      page: shipmentPage.value,
      page_size: pageSize,
    })
    if (version === loadVersion) {
      shipments.value = result.items
      shipmentTotal.value = result.total
    }
  } catch {
    if (version === loadVersion) fail('运单加载失败，请重试')
  } finally {
    if (version === loadVersion) loading.value = false
  }
}

async function loadReturns(version = ++loadVersion): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await listReturns({
      store_id: storeId.value || undefined,
      status: returnFilters.value.status || undefined,
      q: returnFilters.value.q.trim() || undefined,
      page: returnPage.value,
      page_size: pageSize,
    })
    if (version === loadVersion) {
      returns.value = result.items
      returnTotal.value = result.total
    }
  } catch {
    if (version === loadVersion) fail('退货任务加载失败，请重试')
  } finally {
    if (version === loadVersion) loading.value = false
  }
}

async function loadExceptions(version = ++loadVersion): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await listExceptions({
      store_id: storeId.value || undefined,
      status: exceptionFilters.value.status || undefined,
      kind: exceptionFilters.value.kind || undefined,
      page: exceptionPage.value,
      page_size: pageSize,
    })
    if (version === loadVersion) {
      exceptions.value = result.items
      exceptionTotal.value = result.total
    }
  } catch {
    if (version === loadVersion) fail('异常工单加载失败，请重试')
  } finally {
    if (version === loadVersion) loading.value = false
  }
}

async function loadRuns(version = ++loadVersion): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const value = await listAgentRuns(storeId.value || undefined)
    if (version === loadVersion) runs.value = value
  } catch {
    if (version === loadVersion) fail('巡检记录加载失败，请重试')
  } finally {
    if (version === loadVersion) loading.value = false
  }
}

function loadCurrent(): void {
  if (active.value === 'overview') void loadDashboard()
  if (active.value === 'shipments') void loadShipments()
  if (active.value === 'returns') void loadReturns()
  if (active.value === 'exceptions') void loadExceptions()
  if (active.value === 'agent') void loadRuns()
}

function openTab(tab: LogisticsView): void {
  void router.push(logisticsLocation(tab))
}

watch(active, () => { if (route.name === 'logistics') loadCurrent() })

function resetPages(): void {
  shipmentPage.value = 1
  returnPage.value = 1
  exceptionPage.value = 1
}

watch(storeId, () => {
  resetPages()
  loadCurrent()
})

function statusClass(value: string): string {
  if (['delivered', 'closed', 'resolved'].includes(value)) return 'good'
  if (['high', 'rejected'].includes(value)) return 'danger'
  if (['medium', 'open', 'requested', 'pending_dispatch'].includes(value)) return 'warn'
  return 'neutral'
}

function formatPercent(value: number | null): string {
  return value == null ? '—' : `${value}%`
}

async function openShipment(row: Shipment | LogisticsException['shipment_id']): Promise<void> {
  const id = typeof row === 'string' ? row : row.id
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getShipment(id)
  } catch {
    fail('运单详情加载失败')
  } finally {
    detailLoading.value = false
  }
}

function openEvent(): void {
  if (!detail.value) return
  eventInitialTime.value = shanghaiInputValue()
  eventForm.value = {
    action: detail.value.status === 'pending_dispatch' ? 'dispatch' : detail.value.status === 'in_transit' ? 'transit' : 'deliver',
    occurred_at: eventInitialTime.value,
    location: detail.value.last_location ?? '',
    description: '',
  }
  eventOpen.value = true
}

async function saveEvent(): Promise<void> {
  if (!detail.value || !eventForm.value.occurred_at || !eventForm.value.location.trim() || !eventForm.value.description.trim()) {
    ElMessage.warning('请完整填写轨迹信息')
    return
  }
  actionLoading.value = true
  try {
    detail.value = await addShipmentEvent(detail.value.id, { ...eventForm.value, occurred_at: eventForm.value.occurred_at === eventInitialTime.value ? new Date().toISOString() : toUtcIso(eventForm.value.occurred_at), location: eventForm.value.location.trim(), description: eventForm.value.description.trim() })
    eventOpen.value = false
    ElMessage.success('轨迹已记录')
    if (active.value === 'shipments') void loadShipments()
  } catch {
    ElMessage.error('轨迹记录失败，请核对时间与状态')
  } finally {
    actionLoading.value = false
  }
}

function openReturn(): void {
  returnForm.value = { reason: '', expected_return_at: shanghaiInputValue(new Date(Date.now() + 7 * 24 * 60 * 60 * 1000)) }
  returnOpen.value = true
}

async function saveReturn(): Promise<void> {
  if (!detail.value || !returnForm.value.reason.trim() || !returnForm.value.expected_return_at) {
    ElMessage.warning('请填写退货原因和计划退回时间')
    return
  }
  actionLoading.value = true
  try {
    const created = await createReturn({ shipment_id: detail.value.id, reason: returnForm.value.reason.trim(), expected_return_at: toUtcIso(returnForm.value.expected_return_at) })
    detail.value = { ...detail.value, return_case: created }
    returnOpen.value = false
    ElMessage.success('退货任务已创建')
  } catch {
    ElMessage.error('退货任务创建失败')
  } finally {
    actionLoading.value = false
  }
}

async function searchOrders(): Promise<void> {
  orderLoading.value = true
  try {
    orders.value = await listEligibleOrders({ q: orderQuery.value.trim() || undefined, store_id: storeId.value || undefined })
  } catch {
    ElMessage.error('可创建运单的订单加载失败')
  } finally {
    orderLoading.value = false
  }
}

function openCreate(): void {
  createForm.value = { order_id: '', carrier: '', tracking_no: '', destination: '', dispatch_due_at: '', expected_delivery_at: '' }
  orderQuery.value = ''
  createOpen.value = true
  void searchOrders()
}

async function saveShipment(): Promise<void> {
  const form = createForm.value
  if (!form.order_id || !form.carrier.trim() || !form.tracking_no.trim() || !form.destination.trim() || !form.dispatch_due_at || !form.expected_delivery_at) {
    ElMessage.warning('请完整填写运单信息')
    return
  }
  actionLoading.value = true
  try {
    const created = await createShipment({ ...form, carrier: form.carrier.trim(), tracking_no: form.tracking_no.trim(), destination: form.destination.trim(), dispatch_due_at: toUtcIso(form.dispatch_due_at), expected_delivery_at: toUtcIso(form.expected_delivery_at) })
    createOpen.value = false
    ElMessage.success('运单已创建')
    void loadShipments()
    void openShipment(created)
  } catch {
    ElMessage.error('运单创建失败，请核对订单与时间')
  } finally {
    actionLoading.value = false
  }
}

const nextReturnStatuses = computed(() => {
  const status = selectedReturn.value?.status
  if (status === 'requested') return ['approved', 'rejected'] as ReturnStatus[]
  if (status === 'approved') return ['in_transit'] as ReturnStatus[]
  if (status === 'in_transit') return ['received'] as ReturnStatus[]
  if (status === 'received') return ['closed'] as ReturnStatus[]
  return []
})

function openReturnAction(row: ReturnCase): void {
  selectedReturn.value = row
  const next = nextStatus(row.status)
  returnActionInitialTime.value = shanghaiInputValue()
  returnAction.value = { status: next, occurred_at: returnActionInitialTime.value, carrier: row.carrier ?? '', tracking_no: row.tracking_no ?? '', note: '' }
  returnActionOpen.value = true
}

function nextStatus(status: ReturnStatus): Exclude<ReturnStatus, 'requested'> {
  if (status === 'requested') return 'approved'
  if (status === 'approved') return 'in_transit'
  if (status === 'in_transit') return 'received'
  return status === 'received' ? 'closed' : 'rejected'
}

async function saveReturnAction(): Promise<void> {
  if (!selectedReturn.value || !returnAction.value.note.trim()) {
    ElMessage.warning('请填写处理说明')
    return
  }
  if (returnAction.value.status === 'in_transit' && (!returnAction.value.carrier.trim() || !returnAction.value.tracking_no.trim())) {
    ElMessage.warning('退回运输需要承运商和运单号')
    return
  }
  actionLoading.value = true
  try {
    await transitionReturn(selectedReturn.value.id, {
      status: returnAction.value.status,
      occurred_at: returnAction.value.occurred_at === returnActionInitialTime.value ? new Date().toISOString() : toUtcIso(returnAction.value.occurred_at),
      note: returnAction.value.note.trim(),
      ...(returnAction.value.status === 'in_transit' ? { carrier: returnAction.value.carrier.trim(), tracking_no: returnAction.value.tracking_no.trim() } : {}),
    })
    returnActionOpen.value = false
    ElMessage.success('退货状态已更新')
    void loadReturns()
  } catch {
    ElMessage.error('退货状态更新失败，请核对当前状态')
  } finally {
    actionLoading.value = false
  }
}

async function openException(row: LogisticsException): Promise<void> {
  selectedException.value = row
  exceptionAction.value = { action: row.status === 'resolved' ? 'assign' : row.assignee_id ? 'resolve' : 'assign', assignee_id: row.assignee_id ?? '', resolution: '' }
  exceptionOpen.value = true
  try {
    assignees.value = await listAssignees(row.store_id)
  } catch {
    assignees.value = []
  }
}

async function saveException(): Promise<void> {
  if (!selectedException.value) return
  if (exceptionAction.value.action === 'assign' && !exceptionAction.value.assignee_id) {
    ElMessage.warning('请选择处理人')
    return
  }
  if (exceptionAction.value.action === 'resolve' && !exceptionAction.value.resolution.trim()) {
    ElMessage.warning('请填写解决说明')
    return
  }
  actionLoading.value = true
  try {
    await updateException(selectedException.value.id, exceptionAction.value.action === 'assign'
      ? { action: 'assign', assignee_id: exceptionAction.value.assignee_id }
      : { action: 'resolve', resolution: exceptionAction.value.resolution.trim() })
    exceptionOpen.value = false
    ElMessage.success(exceptionAction.value.action === 'assign' ? '工单已分配' : '工单已解决')
    void loadExceptions()
  } catch {
    ElMessage.error('工单更新失败')
  } finally {
    actionLoading.value = false
  }
}

async function patrol(): Promise<void> {
  actionLoading.value = true
  try {
    const run = await runAgentPatrol(storeId.value || undefined)
    ElMessage.success(`巡检完成：新增 ${run.created_exceptions} 个异常`)
    loadCurrent()
  } catch {
    ElMessage.error('规则巡检失败，请稍后重试')
  } finally {
    actionLoading.value = false
  }
}

async function askAgent(useBrief = false): Promise<void> {
  const prompt = useBrief ? '生成当前物流简报' : question.value.trim()
  if (!prompt) return
  if (!useBrief) {
    chat.value.push({ role: 'user', text: prompt })
    question.value = ''
  }
  agentBusy.value = true
  try {
    const answer = useBrief ? await getAgentBrief(storeId.value || undefined) : await queryAgent(prompt, storeId.value || undefined)
    chat.value.push({ role: 'agent', text: answer.answer, answer })
  } catch {
    ElMessage.error('规则查询失败，请换一种问法')
  } finally {
    agentBusy.value = false
  }
}

function download(name: string, headers: string[], rows: unknown[][]): void {
  const url = URL.createObjectURL(new Blob([toCsv(headers, rows)], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `${name}-${new Date().toISOString().slice(0, 10)}.csv`
  link.click()
  URL.revokeObjectURL(url)
  ElMessage.success('已导出当前筛选页')
}

function exportCurrent(): void {
  if (active.value === 'shipments') download('shipments', ['订单号', '运单号', '店铺', '承运商', '状态', '目的地', '预计送达'], shipments.value.map((item) => [item.order_number, item.tracking_no, item.store_name, item.carrier, shipmentStatusLabel[item.status], item.destination, formatShanghai(item.expected_delivery_at)]))
  if (active.value === 'returns') download('returns', ['订单号', '原运单号', '店铺', '状态', '原因', '更新时间'], returns.value.map((item) => [item.order_number, item.shipment_tracking_no, item.store_name, returnStatusLabel[item.status], item.reason, formatShanghai(item.updated_at)]))
  if (active.value === 'exceptions') download('exceptions', ['订单号', '店铺', '类型', '等级', '状态', '负责人'], exceptions.value.map((item) => [item.order_number, item.store_name, exceptionKindLabel[item.kind], item.severity, exceptionStatusLabel[item.status], item.assignee_name ?? '未分配']))
}

onMounted(async () => {
  try {
    stores.value = await listStores()
  } catch {
    fail('店铺范围加载失败')
  }
  loadCurrent()
})
</script>

<template>
  <div class="logistics-page">
    <div class="logistics-main">
      <header class="workspace-header">
        <div>
          <p class="context-line">{{ selectedStoreName }} · {{ today }}</p>
          <h1>物流协同工作台</h1>
        </div>
        <div class="header-actions">
          <span v-if="dashboard?.data_mode === 'demo'" class="demo-label">本地演示数据 · 规则巡检<span v-if="dashboard.agent.last_run_at"> · 最近 {{ formatShanghai(dashboard.agent.last_run_at) }}</span></span>
          <span v-else-if="dashboard?.data_mode === 'mixed'" class="demo-label">包含本地演示数据 · 规则巡检</span>
          <el-select v-model="storeId" clearable placeholder="全部授权店铺" aria-label="选择店铺">
            <el-option v-for="store in stores" :key="store.id" :label="store.name" :value="store.id" />
          </el-select>
          <el-button data-test="patrol-button" :loading="actionLoading" @click="patrol"><el-icon><CircleCheck /></el-icon>运行规则巡检</el-button>
        </div>
      </header>

      <div v-if="error" class="error-banner" role="alert"><span>{{ error }}</span><el-button text @click="loadCurrent">重试</el-button></div>

      <section v-if="active === 'overview'" class="view" aria-labelledby="overview-title">
        <div class="view-heading"><div><p class="eyebrow">OVERVIEW</p><h2 id="overview-title">今天需要处理什么</h2><p>基于当前系统内的运单事件与承诺时间，不代表承运商实时数据。</p></div><span v-if="dashboard">数据截至 {{ formatShanghai(dashboard.as_of) }}</span></div>
        <el-skeleton v-if="loading" :rows="8" animated />
        <el-empty v-else-if="!dashboard || dashboard.data_mode === 'empty'" description="当前范围没有物流数据" />
        <template v-else>
          <div class="metrics-grid">
            <article><span>待发货</span><strong>{{ dashboard.metrics.pending_dispatch }}</strong><small>共 {{ dashboard.metrics.total_shipments }} 票运单</small></article>
            <article><span>运输中</span><strong>{{ dashboard.metrics.in_transit }}</strong><small>持续关注时效节点</small></article>
            <article><span>今日签收</span><strong>{{ dashboard.metrics.delivered_today }}</strong><small>按北京时间统计</small></article>
            <article class="risk-metric"><span>开放异常</span><strong>{{ dashboard.metrics.open_exceptions }}</strong><small>待分配或处理中</small></article>
            <article><span>准时率</span><strong>{{ formatPercent(dashboard.metrics.on_time_rate) }}</strong><small>{{ dashboard.metrics.active_returns }} 个退货处理中</small></article>
          </div>
          <div class="overview-grid">
            <article class="panel flow-panel">
              <div class="panel-title"><div><p class="eyebrow">FLOW</p><h3>运单流转分布</h3></div><button type="button" @click="openTab('shipments')">查看全部</button></div>
              <div class="flow-chart" aria-label="运单状态分布">
                <div v-for="item in dashboard.status_distribution" :key="item.status" class="flow-row">
                  <div class="flow-meta"><span><strong>{{ shipmentStatusLabel[item.status] }}</strong><small>{{ distributionNote[item.status] }}</small></span><span><strong>{{ item.count }}</strong><small>{{ distributionPercent(item.count) }}%</small></span></div>
                  <div class="flow-track"><i :style="{ width: `${distributionPercent(item.count)}%` }" /></div>
                </div>
              </div>
            </article>
            <article class="panel risk-panel">
              <div class="panel-title"><div><p class="eyebrow">ACTION QUEUE</p><h3>优先风险</h3></div><button type="button" @click="openTab('exceptions')">进入工单</button></div>
              <button v-for="item in dashboard.recent_exceptions.slice(0, 4)" :key="item.id" class="risk-row" type="button" @click="openShipment(item.shipment_id)">
                <span :class="['status-dot', item.severity]" /><span><strong>{{ item.title }}</strong><small>{{ item.store_name }} · {{ item.order_number }}</small></span><span>{{ exceptionKindLabel[item.kind] }}</span>
              </button>
              <p v-if="!dashboard.recent_exceptions.length" class="quiet-state">当前没有开放异常</p>
            </article>
            <article class="panel carrier-panel">
              <div class="panel-title"><div><p class="eyebrow">CARRIERS</p><h3>承运商表现</h3></div><small>系统记录口径</small></div>
              <table><thead><tr><th>承运商</th><th>运单</th><th>已签收</th><th>准时率</th></tr></thead><tbody><tr v-for="item in dashboard.carriers" :key="item.carrier"><td>{{ item.carrier }}</td><td>{{ item.total }}</td><td>{{ item.delivered }}</td><td><strong>{{ formatPercent(item.on_time_rate) }}</strong></td></tr></tbody></table>
            </article>
            <article class="panel events-panel">
              <div class="panel-title"><div><p class="eyebrow">ACTIVITY</p><h3>最近事件</h3></div></div>
              <ol class="mini-timeline"><li v-for="event in dashboard.recent_events.slice(0, 5)" :key="event.id"><i /><span><strong>{{ overviewEventDescription(event.description) }}</strong><small>{{ event.location }} · {{ formatShanghai(event.occurred_at) }}</small></span></li></ol>
            </article>
          </div>
        </template>
      </section>

      <section v-else-if="active === 'shipments'" class="view" aria-labelledby="shipments-title">
        <div class="view-heading"><div><p class="eyebrow">SHIPMENTS</p><h2 id="shipments-title">运单追踪</h2><p>查看承诺时间、实际节点与异常上下文。</p></div><div class="view-actions"><el-button :icon="Download" @click="exportCurrent">导出当前页</el-button><el-button type="primary" @click="openCreate">新建运单</el-button></div></div>
        <div class="filter-bar">
          <el-input v-model="shipmentFilters.q" data-test="shipment-search" clearable aria-label="搜索订单号或运单号" placeholder="搜索订单号 / 运单号" @keyup.enter="shipmentPage = 1; loadShipments()" />
          <el-select v-model="shipmentFilters.status" clearable placeholder="全部状态" aria-label="运单状态"><el-option v-for="(label, key) in shipmentStatusLabel" :key="key" :label="label" :value="key" /></el-select>
          <el-checkbox v-model="shipmentFilters.risk_only">只看风险</el-checkbox>
          <el-button @click="shipmentPage = 1; loadShipments()">筛选</el-button>
        </div>
        <el-skeleton v-if="loading" :rows="7" animated />
        <el-empty v-else-if="!shipments.length" description="没有符合条件的运单" />
        <template v-else>
          <div class="table-wrap"><el-table :data="shipments" table-layout="fixed" @row-click="openShipment">
            <el-table-column label="订单 / 运单" min-width="178"><template #default="{ row }"><div class="primary-cell"><strong>{{ row.order_number }}</strong><small>{{ row.tracking_no }}</small></div></template></el-table-column>
            <el-table-column prop="store_name" label="店铺" min-width="128" />
            <el-table-column prop="carrier" label="承运商" width="100" />
            <el-table-column label="状态" width="104"><template #default="{ row }"><span :class="['status-chip', statusClass(row.status)]">{{ shipmentStatusLabel[row.status as ShipmentStatus] }}</span></template></el-table-column>
            <el-table-column label="当前位置" min-width="150"><template #default="{ row }">{{ row.last_location || '等待首条轨迹' }}</template></el-table-column>
            <el-table-column label="预计送达" width="154"><template #default="{ row }">{{ formatShanghai(row.expected_delivery_at) }}</template></el-table-column>
            <el-table-column label="风险" width="96"><template #default="{ row }"><span :class="['status-chip', statusClass(row.risk_level)]">{{ row.risk_level === 'normal' ? '正常' : row.risk_level === 'high' ? '高风险' : '关注' }}</span></template></el-table-column>
            <el-table-column width="104" label="操作"><template #default="{ row }"><el-button text @click.stop="openShipment(row)">查看详情</el-button></template></el-table-column>
          </el-table></div>
          <el-pagination background layout="prev, pager, next, total" :current-page="shipmentPage" :page-size="pageSize" :total="shipmentTotal" @current-change="(page: number) => { shipmentPage = page; loadShipments() }" />
        </template>
      </section>

      <section v-else-if="active === 'returns'" class="view" aria-labelledby="returns-title">
        <div class="view-heading"><div><p class="eyebrow">RETURNS</p><h2 id="returns-title">退货管理</h2><p>推进逆向物流；完成仅代表物流闭环，不执行退款。</p></div><div class="view-actions"><span class="count-note">本页 {{ activeReturns }} 个处理中</span><el-button :icon="Download" @click="exportCurrent">导出当前页</el-button></div></div>
        <div class="filter-bar"><el-input v-model="returnFilters.q" clearable aria-label="搜索退货订单" placeholder="搜索订单号 / 运单号" @keyup.enter="returnPage = 1; loadReturns()" /><el-select v-model="returnFilters.status" clearable placeholder="全部状态" aria-label="退货状态"><el-option v-for="(label, key) in returnStatusLabel" :key="key" :label="label" :value="key" /></el-select><el-button @click="returnPage = 1; loadReturns()">筛选</el-button></div>
        <el-skeleton v-if="loading" :rows="7" animated />
        <el-empty v-else-if="!returns.length" description="没有符合条件的退货任务" />
        <template v-else><div class="table-wrap"><el-table :data="returns" table-layout="fixed">
          <el-table-column label="订单 / 原运单" min-width="180"><template #default="{ row }"><div class="primary-cell"><strong>{{ row.order_number }}</strong><small>{{ row.shipment_tracking_no }}</small></div></template></el-table-column>
          <el-table-column prop="store_name" label="店铺" min-width="130" />
          <el-table-column label="状态" width="110"><template #default="{ row }"><span :class="['status-chip', statusClass(row.status)]">{{ returnStatusLabel[row.status as ReturnStatus] }}</span></template></el-table-column>
          <el-table-column prop="reason" label="退货原因" min-width="190" show-overflow-tooltip />
          <el-table-column label="退回运单" min-width="150"><template #default="{ row }">{{ row.carrier && row.tracking_no ? `${row.carrier} · ${row.tracking_no}` : '待登记' }}</template></el-table-column>
          <el-table-column label="更新时间" width="154"><template #default="{ row }">{{ formatShanghai(row.updated_at) }}</template></el-table-column>
          <el-table-column label="操作" width="110"><template #default="{ row }"><el-button v-if="!['closed', 'rejected'].includes(row.status)" text @click="openReturnAction(row)">推进处理</el-button><span v-else class="muted">已归档</span></template></el-table-column>
        </el-table></div><el-pagination background layout="prev, pager, next, total" :current-page="returnPage" :page-size="pageSize" :total="returnTotal" @current-change="(page: number) => { returnPage = page; loadReturns() }" /></template>
      </section>

      <section v-else-if="active === 'exceptions'" class="view" aria-labelledby="exceptions-title">
        <div class="view-heading"><div><p class="eyebrow">EXCEPTIONS</p><h2 id="exceptions-title">异常工单</h2><p>从规则证据进入运单事实，再分配和关闭任务。</p></div><el-button :icon="Download" @click="exportCurrent">导出当前页</el-button></div>
        <div class="filter-bar"><el-select v-model="exceptionFilters.status" clearable placeholder="全部状态" aria-label="工单状态"><el-option v-for="(label, key) in exceptionStatusLabel" :key="key" :label="label" :value="key" /></el-select><el-select v-model="exceptionFilters.kind" clearable placeholder="全部类型" aria-label="异常类型"><el-option v-for="(label, key) in exceptionKindLabel" :key="key" :label="label" :value="key" /></el-select><el-button @click="exceptionPage = 1; loadExceptions()">筛选</el-button></div>
        <el-skeleton v-if="loading" :rows="7" animated />
        <el-empty v-else-if="!exceptions.length" description="当前没有异常工单" />
        <template v-else><div class="exception-list"><article v-for="item in exceptions" :key="item.id" class="exception-card">
          <div class="exception-severity"><span :class="['status-dot', item.severity]" /><span>{{ item.severity === 'high' ? '高优先级' : '中优先级' }}</span></div>
          <div class="exception-copy"><div><span :class="['status-chip', statusClass(item.status)]">{{ exceptionStatusLabel[item.status] }}</span><small>{{ exceptionKindLabel[item.kind] }}</small></div><h3>{{ item.title }}</h3><p>{{ item.description }}</p><footer><span>{{ item.store_name }} · {{ item.order_number }}</span><span>负责人：{{ item.assignee_name || '未分配' }}</span><span>{{ formatShanghai(item.created_at) }}</span></footer></div>
          <div class="exception-actions"><el-button @click="openShipment(item.shipment_id)">核对运单</el-button><el-button v-if="item.status !== 'resolved'" type="primary" @click="openException(item)">{{ item.assignee_id ? '处理工单' : '分配工单' }}</el-button></div>
        </article></div><el-pagination background layout="prev, pager, next, total" :current-page="exceptionPage" :page-size="pageSize" :total="exceptionTotal" @current-change="(page: number) => { exceptionPage = page; loadExceptions() }" /></template>
      </section>

      <section v-else class="view agent-view" aria-labelledby="agent-title">
        <div class="view-heading"><div><p class="eyebrow">RULE OPERATIONS</p><h2 id="agent-title">规则巡检 Agent</h2><p>使用确定性规则分析本系统数据；不连接外部承运商，也不调用外部大模型。</p></div><el-button :loading="actionLoading" type="primary" @click="patrol">立即巡检</el-button></div>
        <div class="agent-grid">
          <article class="panel chat-panel"><div class="panel-title"><div><p class="eyebrow">GROUNDED QUERY</p><h3>物流问答</h3></div><el-button text :disabled="agentBusy" @click="askAgent(true)">生成今日简报</el-button></div>
            <div class="chat-stream"><div v-if="!chat.length" class="agent-welcome"><ChatLineRound /><strong>从事实开始提问</strong><p>例如：哪些运单已超过发货承诺？请给出订单引用。</p></div><article v-for="(message, index) in chat" :key="index" :class="['message', message.role]"><small>{{ message.role === 'user' ? '你' : '规则巡检 Agent' }}</small><p>{{ message.text }}</p><div v-if="message.answer?.citations.length" class="citations"><button v-for="citation in message.answer.citations" :key="citation.shipment_id" type="button" @click="openShipment(citation.shipment_id)"><strong>{{ citation.order_number }}</strong><span>{{ citation.detail }}</span></button></div><div v-if="message.answer?.suggestions.length" class="suggestions"><span v-for="item in message.answer.suggestions" :key="item">{{ item }}</span></div></article><p v-if="agentBusy" class="typing">正在按规则检索系统记录…</p></div>
            <div class="chat-input"><el-input v-model="question" aria-label="向规则巡检 Agent 提问" placeholder="询问当前物流风险" @keyup.enter="askAgent(false)" /><el-button type="primary" :loading="agentBusy" :disabled="!question.trim()" @click="askAgent(false)">发送</el-button></div>
          </article>
          <article class="panel run-panel"><div class="panel-title"><div><p class="eyebrow">PATROL HISTORY</p><h3>巡检历史</h3></div><span class="mode-badge">DETERMINISTIC RULES</span></div><el-skeleton v-if="loading" :rows="5" animated /><ol v-else class="run-list"><li v-for="run in runs" :key="run.id"><i /><div><strong>{{ run.summary }}</strong><p>扫描 {{ run.scanned_shipments }} 票 · 新增 {{ run.created_exceptions }} · 已存在 {{ run.existing_exceptions }}</p><small>{{ formatShanghai(run.completed_at) }}</small></div></li></ol><p v-if="!loading && !runs.length" class="quiet-state">尚无巡检记录</p></article>
        </div>
      </section>
    </div>

    <el-drawer v-model="detailOpen" data-test="shipment-detail" size="min(560px, 100%)" title="运单详情">
      <el-skeleton v-if="detailLoading" :rows="9" animated />
      <div v-else-if="detail" class="detail-body">
        <div class="detail-hero"><div><p>{{ detail.store_name }} · {{ detail.order_number }}</p><h2>{{ detail.tracking_no }}</h2><span>{{ detail.carrier }} → {{ detail.destination }}</span></div><span :class="['status-chip', statusClass(detail.status)]">{{ shipmentStatusLabel[detail.status] }}</span></div>
        <div class="detail-actions"><el-button v-if="detail.status !== 'delivered'" type="primary" @click="openEvent">新增物流节点</el-button><el-button v-if="!detail.return_case && detail.status !== 'pending_dispatch'" @click="openReturn">发起退货</el-button></div>
        <dl class="fact-grid"><div><dt>计划发货</dt><dd>{{ formatShanghai(detail.dispatch_due_at) }}</dd></div><div><dt>实际发货</dt><dd>{{ formatShanghai(detail.dispatched_at) }}</dd></div><div><dt>预计送达</dt><dd>{{ formatShanghai(detail.expected_delivery_at) }}</dd></div><div><dt>实际签收</dt><dd>{{ formatShanghai(detail.delivered_at) }}</dd></div></dl>
        <section v-if="detail.return_case" class="linked-return"><p class="eyebrow">RETURN CASE</p><div><strong>退货任务 {{ returnStatusLabel[detail.return_case.status] }}</strong><span>{{ detail.return_case.reason }}</span></div></section>
        <section><div class="section-title"><h3>物流事件</h3><span>{{ detail.events.length }} 条记录</span></div><el-empty v-if="!detail.events.length" description="尚无物流事件" :image-size="64" /><ol v-else class="timeline"><li v-for="event in detail.events" :key="event.id"><i /><div><small>{{ formatShanghai(event.occurred_at) }}</small><strong>{{ event.description }}</strong><span>{{ event.location }} · {{ event.actor_name }} · {{ event.source === 'demo' ? '演示记录' : event.source === 'agent' ? '规则生成' : '人工录入' }}</span></div></li></ol></section>
        <section v-if="detail.exceptions.length"><div class="section-title"><h3>关联异常</h3></div><article v-for="item in detail.exceptions" :key="item.id" class="detail-exception"><span :class="['status-dot', item.severity]" /><div><strong>{{ item.title }}</strong><p>{{ item.recommended_action }}</p></div></article></section>
      </div>
    </el-drawer>

    <el-dialog v-model="eventOpen" title="新增物流节点" width="min(520px, 94vw)"><div class="dialog-form"><label>节点类型<el-select v-model="eventForm.action" aria-label="节点类型"><el-option v-if="detail?.status === 'pending_dispatch'" label="确认发货" value="dispatch" /><template v-if="detail?.status === 'in_transit'"><el-option label="更新运输轨迹" value="transit" /><el-option label="确认签收" value="deliver" /></template></el-select></label><label>发生时间（北京时间）<input v-model="eventForm.occurred_at" type="datetime-local" step="1" /></label><label>地点<el-input v-model="eventForm.location" placeholder="例如：杭州转运中心" /></label><label>节点说明<el-input v-model="eventForm.description" type="textarea" :rows="3" placeholder="记录可核对的物流事实" /></label></div><template #footer><el-button @click="eventOpen = false">取消</el-button><el-button type="primary" :loading="actionLoading" @click="saveEvent">保存节点</el-button></template></el-dialog>

    <el-dialog v-model="returnOpen" title="发起退货" width="min(500px, 94vw)"><p class="dialog-note">创建逆向物流任务，不会触发退款或库存变更。</p><div class="dialog-form"><label>退货原因<el-input v-model="returnForm.reason" type="textarea" :rows="3" placeholder="说明退货原因" /></label><label>计划退回时间（北京时间）<input v-model="returnForm.expected_return_at" type="datetime-local" step="1" /></label></div><template #footer><el-button @click="returnOpen = false">取消</el-button><el-button type="primary" :loading="actionLoading" @click="saveReturn">创建退货任务</el-button></template></el-dialog>

    <el-dialog v-model="createOpen" title="新建运单" width="min(620px, 94vw)"><div class="order-search"><el-input v-model="orderQuery" aria-label="搜索可创建运单的订单" placeholder="搜索可创建运单的订单" @keyup.enter="searchOrders" /><el-button :loading="orderLoading" @click="searchOrders">搜索</el-button></div><div class="dialog-form two-column"><label class="full">选择订单<el-select v-model="createForm.order_id" filterable placeholder="选择未建运单的订单" aria-label="选择订单"><el-option v-for="order in orders" :key="order.id" :label="`${order.order_number ?? order.id} · ${order.store_name}`" :value="order.id" /></el-select></label><label>承运商<el-input v-model="createForm.carrier" placeholder="例如：顺丰速运" /></label><label>运单号<el-input v-model="createForm.tracking_no" /></label><label class="full">目的地<el-input v-model="createForm.destination" placeholder="城市 / 区县" /></label><label>计划发货（北京时间）<input v-model="createForm.dispatch_due_at" type="datetime-local" step="1" /></label><label>预计送达（北京时间）<input v-model="createForm.expected_delivery_at" type="datetime-local" step="1" /></label></div><template #footer><el-button @click="createOpen = false">取消</el-button><el-button type="primary" :loading="actionLoading" @click="saveShipment">创建运单</el-button></template></el-dialog>

    <el-dialog v-model="returnActionOpen" title="推进退货任务" width="min(540px, 94vw)"><div class="dialog-form"><label>下一状态<el-select v-model="returnAction.status" aria-label="退货下一状态"><el-option v-for="status in nextReturnStatuses" :key="status" :label="returnStatusLabel[status]" :value="status" /></el-select></label><label>发生时间（北京时间）<input v-model="returnAction.occurred_at" type="datetime-local" step="1" /></label><template v-if="returnAction.status === 'in_transit'"><label>承运商<el-input v-model="returnAction.carrier" /></label><label>退回运单号<el-input v-model="returnAction.tracking_no" /></label></template><label>处理说明<el-input v-model="returnAction.note" type="textarea" :rows="3" placeholder="记录本次处理事实" /></label></div><template #footer><el-button @click="returnActionOpen = false">取消</el-button><el-button type="primary" :loading="actionLoading" @click="saveReturnAction">确认推进</el-button></template></el-dialog>

    <el-dialog v-model="exceptionOpen" title="处理异常工单" width="min(520px, 94vw)"><div v-if="selectedException" class="dialog-summary"><strong>{{ selectedException.title }}</strong><p>{{ selectedException.recommended_action }}</p></div><div class="dialog-form"><label>处理动作<el-radio-group v-model="exceptionAction.action" aria-label="工单处理动作"><el-radio value="assign">分配处理人</el-radio><el-radio value="resolve">标记已解决</el-radio></el-radio-group></label><label v-if="exceptionAction.action === 'assign'">处理人<el-select v-model="exceptionAction.assignee_id" placeholder="选择处理人" aria-label="选择处理人"><el-option v-for="person in assignees" :key="person.id" :label="person.username" :value="person.id" /></el-select></label><label v-else>解决说明<el-input v-model="exceptionAction.resolution" type="textarea" :rows="3" placeholder="说明核对结果与处理动作" /></label></div><template #footer><el-button @click="exceptionOpen = false">取消</el-button><el-button type="primary" :loading="actionLoading" @click="saveException">保存处理结果</el-button></template></el-dialog>
  </div>
</template>

<style scoped>
 .workspace-header { display: flex; justify-content: space-between; border-bottom: 1px solid var(--app-border); min-height: 0; padding: 0 0 24px; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
.context-line { margin: 0 0 5px; color: var(--app-muted); font-size: 12px; }
.workspace-header h1 { margin: 0; font-family: inherit; letter-spacing: -0.02em; font-size: 22px; font-weight: 650; }
.header-actions,.view-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.header-actions .el-select { width: 190px; }
.demo-label,.mode-badge { padding: 5px 8px; border-radius: 999px; background: #fbf3db; color: #85600b; font: 700 10px/1.2 "SF Mono",monospace; letter-spacing: .04em; }
.error-banner { display: flex; align-items: center; justify-content: space-between; margin: 18px 0; padding: 10px 14px; border: 1px solid #f2d3d3; border-radius: 7px; background: #fdebec; color: #8c302e; font-size: 13px; }
.view { padding-top: 24px; }
.view-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 28px; margin-bottom: 20px; }
.view-heading h2 { margin: 0; font-family: inherit; letter-spacing: -0.035em; font-size: 27px; line-height: 1.25; }
.view-heading p:not(.eyebrow) { margin: 7px 0 0; color: var(--app-muted); font-size: 14px; }
.view-heading>span { color: var(--app-muted); font-size: 12px; }
.eyebrow { margin: 0 0 7px; color: var(--app-accent); font: 700 10px/1.2 "SF Mono",monospace; letter-spacing: .12em; }
.metrics-grid { display: grid; grid-template-columns: repeat(5,minmax(0,1fr)); margin-bottom: 18px; border: 1px solid var(--app-border); border-radius: 10px; background: var(--app-surface); overflow: hidden; }
.metrics-grid article { display: grid; gap: 6px; border-right: 1px solid var(--app-border); padding: 19px 20px; }
.metrics-grid article:last-child { border-right: 0; }
.metrics-grid span { color: var(--app-muted); font-size: 12px; }
.metrics-grid strong { font-family: inherit; font-weight: 650; font-variant-numeric: tabular-nums; letter-spacing: -0.045em; font-size: 32px; }
.metrics-grid small { font-size: 11px; color: var(--app-muted); }
.metrics-grid .risk-metric strong { color: #a64d42; }
.overview-grid { display: grid; grid-template-columns: minmax(0,1.2fr) minmax(330px,.8fr); gap: 18px; align-items: start; }
.panel { border: 1px solid var(--app-border); border-radius: 10px; background: var(--app-surface); align-self: start; padding: 20px 22px; }
.panel-title { display: flex; align-items: start; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.panel-title h3 { margin: 0; font-family: inherit; letter-spacing: -0.02em; font-size: 18px; }
.panel-title button { border: 0; background: none; color: var(--app-accent); font-family: inherit; font-size: 12px; font-weight: 600; line-height: 1.4; cursor: pointer; }
.panel-title>small { color: var(--app-muted); }
.flow-chart { display: grid; gap: 0; }
.flow-row { font-size: 12px; display: block; padding: 13px 0; border-top: 1px solid var(--app-border); }
.flow-row>div { height: 8px; background: #eff1ee; }
.flow-row i { display: block; height: 100%; background: #6f9f98; }
.flow-row strong { text-align: right; }
.risk-row { display: grid; grid-template-columns: 9px 1fr auto; align-items: center; gap: 12px; width: 100%; border: 0; border-top: 1px solid var(--app-border); background: none; color: inherit; text-align: left; cursor: pointer; padding: 10px 0; }
.risk-row>span:nth-child(2) { display: grid; gap: 4px; }
.risk-row small { color: var(--app-muted); }
.risk-row>span:last-child { color: #8a6152; font-size: 11px; }
.status-dot { display: block; width: 8px; height: 8px; border-radius: 50%; background: #d2a755; }
.status-dot.high { background: #bb594e; }
.status-dot.medium { background: #d2a755; }
.carrier-panel table { width: 100%; border-collapse: collapse; font-size: 13px; }
.carrier-panel th,.carrier-panel td { padding: 12px 4px; border-bottom: 1px solid var(--app-border); text-align: left; }
.carrier-panel th { color: var(--app-muted); font-size: 11px; font-weight: 500; }
.mini-timeline,.timeline,.run-list { margin: 0; padding: 0; list-style: none; }
.mini-timeline li { display: grid; grid-template-columns: 10px 1fr; gap: 12px; padding: 0 0 17px; padding-bottom: 14px; }
.mini-timeline i,.timeline i,.run-list i { width: 7px; height: 7px; margin-top: 5px; border-radius: 50%; background: #86aaa4; }
.mini-timeline span { display: grid; gap: 4px; }
.mini-timeline strong { font-size: 12px; font-weight: 600; line-height: 1.45; overflow-wrap: anywhere; }
.mini-timeline small { color: var(--app-muted); font-size: 11px; }
.quiet-state { color: var(--app-muted); font-size: 13px; text-align: center; }
.filter-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.filter-bar .el-input { width: min(330px,35vw); }
.filter-bar .el-select { width: 160px; }
.table-wrap { border: 1px solid var(--app-border); border-radius: 9px; overflow: hidden; }
.primary-cell { display: grid; gap: 4px; }
.primary-cell small { color: var(--app-muted); font: 11px/1.4 "SF Mono",monospace; }
.status-chip { display: inline-flex; width: max-content; padding: 4px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; white-space: nowrap; }
.status-chip.good { background: #edf3ec; color: #346538; }
.status-chip.warn { background: #fbf3db; color: #8a650c; }
.status-chip.danger { background: #fdebec; color: #9f2f2d; }
.status-chip.neutral { background: #e9f3f1; color: #26675f; }
.el-pagination { justify-content: flex-end; margin-top: 18px; }
.count-note { color: var(--app-muted); font-size: 12px; }
.exception-list { display: grid; gap: 10px; }
.exception-card { display: grid; grid-template-columns: 90px 1fr auto; gap: 18px; padding: 19px; border: 1px solid var(--app-border); border-radius: 9px; background: #fff; }
.exception-severity { display: flex; align-items: center; gap: 7px; color: var(--app-muted); font-size: 11px; }
.exception-copy>div { display: flex; align-items: center; gap: 8px; }
.exception-copy>div small { color: var(--app-muted); }
.exception-copy h3 { margin: 9px 0 5px; font-size: 15px; }
.exception-copy p { margin: 0; color: #626763; font-size: 13px; }
.exception-copy footer { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 13px; color: var(--app-muted); font-size: 11px; }
.exception-actions { display: flex; align-items: center; gap: 8px; }
.agent-grid { display: grid; grid-template-columns: minmax(0,1.35fr) minmax(320px,.65fr); gap: 18px; }
.chat-panel { display: flex; min-height: 610px; flex-direction: column; }
.chat-stream { display: grid; align-content: start; gap: 16px; min-height: 390px; max-height: 520px; padding: 4px; overflow: auto; }
.agent-welcome { display: grid; justify-items: center; margin: auto; padding: 60px 20px; color: var(--app-muted); text-align: center; }
.agent-welcome svg { width: 34px; margin-bottom: 14px; color: #8aaaa5; }
.agent-welcome p { max-width: 330px; }
.message { max-width: 88%; padding: 13px 15px; border-radius: 8px; background: #f4f5f2; }
.message.user { justify-self: end; background: #e8f1ef; }
.message small { color: var(--app-muted); }
.message p { margin: 5px 0 0; line-height: 1.65; white-space: pre-wrap; }
.citations { display: grid; gap: 6px; margin-top: 12px; }
.citations button { display: grid; gap: 2px; padding: 10px; border: 1px solid #dde4e0; border-radius: 6px; background: #fff; color: inherit; text-align: left; cursor: pointer; }
.citations span { color: var(--app-muted); font-size: 11px; }
.suggestions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.suggestions span { padding: 4px 7px; border-radius: 999px; background: #fbf3db; color: #7e621b; font-size: 10px; }
.typing { color: var(--app-muted); font-size: 12px; }
.chat-input { display: flex; gap: 8px; margin-top: auto; padding-top: 18px; border-top: 1px solid var(--app-border); }
.mode-badge { background: #edf3ec; color: #346538; }
.run-list li { display: grid; grid-template-columns: 10px 1fr; gap: 12px; padding: 15px 0; border-bottom: 1px solid var(--app-border); }
.run-list p { margin: 5px 0; color: var(--app-muted); font-size: 12px; }
.run-list small { color: #989d99; }
.detail-body { display: grid; gap: 26px; }
.detail-hero { display: flex; align-items: start; justify-content: space-between; gap: 20px; padding-bottom: 22px; border-bottom: 1px solid var(--app-border); }
.detail-hero p { margin: 0 0 6px; color: var(--app-muted); font-size: 12px; }
.detail-hero h2 { margin: 0 0 7px; font: 700 23px/1.2 "SF Mono",monospace; }
.detail-hero>div>span { color: var(--app-muted); font-size: 13px; }
.detail-actions { display: flex; gap: 9px; }
.fact-grid { display: grid; grid-template-columns: repeat(2,1fr); margin: 0; border: 1px solid var(--app-border); border-radius: 8px; }
.fact-grid div { padding: 14px; border-right: 1px solid var(--app-border); border-bottom: 1px solid var(--app-border); }
.fact-grid div:nth-child(2n) { border-right: 0; }
.fact-grid div:nth-last-child(-n+2) { border-bottom: 0; }
.fact-grid dt { color: var(--app-muted); font-size: 11px; }
.fact-grid dd { margin: 5px 0 0; font-size: 13px; }
.linked-return { padding: 15px; border: 1px solid #dce8e4; border-radius: 8px; background: #f4f8f6; }
.linked-return div { display: grid; gap: 5px; }
.linked-return span { color: var(--app-muted); font-size: 12px; }
.section-title { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.section-title h3 { margin: 0; font-size: 15px; }
.section-title span { color: var(--app-muted); font-size: 11px; }
.timeline li { position: relative; display: grid; grid-template-columns: 11px 1fr; gap: 13px; padding: 0 0 22px; }
.timeline li:not(:last-child)::after { position: absolute; top: 12px; bottom: 3px; left: 3px; width: 1px; background: #dce2df; content: ""; }
.timeline div { display: grid; gap: 5px; }
.timeline small,.timeline span { color: var(--app-muted); font-size: 11px; }
.timeline strong { font-size: 13px; }
.detail-exception { display: flex; gap: 12px; padding: 13px; border-top: 1px solid var(--app-border); }
.detail-exception p { margin: 4px 0; color: var(--app-muted); font-size: 12px; }
.dialog-note,.dialog-summary { margin: 0 0 18px; padding: 12px; border: 1px solid var(--app-border); border-radius: 7px; background: #f8f8f6; color: var(--app-muted); font-size: 12px; }
.dialog-summary p { margin: 5px 0 0; }
.dialog-form { display: grid; gap: 16px; }
.dialog-form.two-column { grid-template-columns: 1fr 1fr; }
.dialog-form label { display: grid; gap: 7px; color: #555a56; font-size: 12px; font-weight: 600; }
.dialog-form .full { grid-column: 1/-1; }
.dialog-form input[type="datetime-local"] { width: 100%; height: 32px; padding: 0 10px; border: 1px solid #dcdfe6; border-radius: 4px; color: var(--app-text); font: inherit; }
.order-search { display: flex; gap: 8px; margin-bottom: 18px; }
:deep(.el-table) { --el-table-border-color: var(--app-border); --el-table-header-bg-color: #f7f7f4; --el-table-row-hover-bg-color: #f5f8f6; --el-table-text-color: var(--app-text); --el-table-header-text-color: #6e746f; }
:deep(.el-drawer__header) { margin-bottom: 0; padding-bottom: 18px; border-bottom: 1px solid var(--app-border); }
.logistics-page { min-width: 0; }
.header-actions { margin-left: auto; max-width: 100%; }
@media (max-width:1000px) {
  .metrics-grid { grid-template-columns: repeat(3,1fr); }
  .metrics-grid article:nth-child(3) { border-right: 0; }
  .metrics-grid article { border-bottom: 1px solid var(--app-border); }
  .overview-grid,.agent-grid { grid-template-columns: 1fr; }
  .exception-card { grid-template-columns: 80px 1fr; }
  .exception-actions { grid-column: 2; }
  .header-actions { flex-wrap: wrap; justify-content: flex-end; }
}
@media (max-width:767px) {
  .workspace-header { display: block; min-height: 0; padding: 0 0 18px; }
  .workspace-header h1 { font-size: 20px; }
  .header-actions { display: grid; grid-template-columns: minmax(0, 1fr) auto; justify-items: stretch; width: 100%; margin: 16px 0 0; }
  .header-actions .el-select { width: 100%; min-width: 0; }
  .header-actions .demo-label { display: none; }
  .view { padding-top: 24px; }
  .view-heading { align-items: start; }
  .view-heading h2 { font-size: 27px; }
  .view-heading>span,.view-heading p:not(.eyebrow) { display: none; }
  .view-actions { flex-wrap: wrap; justify-content: flex-end; }
  .metrics-grid { grid-template-columns: repeat(2,1fr); }
  .metrics-grid article:nth-child(2n) { border-right: 0; }
  .metrics-grid article:nth-child(3) { border-right: 1px solid var(--app-border); }
  .metrics-grid article:last-child { grid-column: 1/-1; border-bottom: 0; }
  .overview-grid { display: block; }
  .overview-grid .panel { margin-bottom: 12px; }
  .filter-bar { display: grid; grid-template-columns: 1fr 1fr; }
  .filter-bar .el-input { grid-column: 1/-1; width: 100%; }
  .filter-bar .el-select { width: 100%; }
  .table-wrap { overflow-x: auto; }
  .table-wrap .el-table { min-width: 820px; }
  .exception-card { grid-template-columns: 1fr; }
  .exception-severity,.exception-actions { grid-column: 1; }
  .exception-actions { justify-content: flex-end; }
  .agent-grid { display: block; }
  .run-panel { margin-top: 12px; }
  .chat-panel { min-height: 520px; }
  .message { max-width: 96%; }
  .dialog-form.two-column { grid-template-columns: 1fr; }
  .dialog-form .full { grid-column: auto; }
  .fact-grid { grid-template-columns: 1fr; }
  .fact-grid div { border-right: 0; }
  .fact-grid div:nth-last-child(2) { border-bottom: 1px solid var(--app-border); }
}
@media (prefers-reduced-motion:reduce) {
  * { scroll-behavior: auto !important; transition: none !important; }
}
.demo-label,
.mode-badge,
.eyebrow { font-family: inherit; }
.workspace-header h1,
.view-heading h2,
.panel-title h3 { font-family: inherit; font-weight: 700; }
.flow-row:first-child { padding-top: 0; border-top: 0; }
.flow-row:last-child { padding-bottom: 0; }
.flow-row > .flow-meta { display: flex; height: auto; align-items: end; justify-content: space-between; gap: 18px; margin-bottom: 10px; background: transparent; }
.flow-meta > span { display: grid; gap: 3px; }
.flow-meta > span:last-child { justify-items: end; }
.flow-meta strong { color: var(--app-text); font-size: 13px; font-weight: 650; text-align: left; }
.flow-meta > span:last-child strong { font-size: 20px; font-variant-numeric: tabular-nums; text-align: right; }
.flow-meta small { color: var(--app-muted); font-size: 11px; }
.flow-row > .flow-track { height: 7px; background: #eff1ee; }
.primary-cell small,
.detail-hero h2 { font-family: inherit; font-variant-numeric: tabular-nums; }
@media (max-width: 767px) {
  .workspace-header { min-height: 0; }
}
</style>
