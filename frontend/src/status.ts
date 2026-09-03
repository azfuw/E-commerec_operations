import type {
  WorkbenchAction,
  WorkflowQuality,
  WorkflowStatus,
} from './types'

export type StatusMeta = {
  label: string
  tag: 'primary' | 'success' | 'warning' | 'info' | 'danger'
  action: string
}

export const workflowStatus: Record<WorkflowStatus, StatusMeta> = {
  accepted: { label: '已受理', tag: 'info', action: '等待处理' },
  processing: { label: '处理中', tag: 'primary', action: '查看进度' },
  awaiting_selection: { label: '等待选品', tag: 'warning', action: '选择商品' },
  completed: { label: '已完成', tag: 'success', action: '查看结果' },
  draft_ready: { label: '草稿就绪', tag: 'success', action: '提交审批' },
  pending_manual: { label: '等待修订', tag: 'warning', action: '修订方案' },
  pending_approval: { label: '等待审批', tag: 'warning', action: '审核方案' },
  rejected: { label: '已驳回', tag: 'danger', action: '查看结果' },
  failed: { label: '处理失败', tag: 'danger', action: '查看异常' },
}

export const workflowQuality: Record<
  WorkflowQuality,
  Pick<StatusMeta, 'label' | 'tag'>
> = {
  normal: { label: '质量正常', tag: 'success' },
  partial: { label: '部分结果', tag: 'warning' },
  degraded: { label: '降级', tag: 'danger' },
}

export const workbenchAction: Record<WorkbenchAction, string> = {
  wait: '等待处理',
  select_product: '选择商品',
  edit_proposal: '修订方案',
  submit_proposal: '提交审批',
  review_approval: '审核方案',
  view_result: '查看结果',
  resolve_failure: '查看异常',
}
