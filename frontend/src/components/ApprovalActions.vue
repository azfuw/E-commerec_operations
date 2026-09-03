<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { ElMessageBox } from 'element-plus'

import { approveProposal, rejectProposal, requestProposalChanges } from '../api'
import { canApprove } from '../capabilities'
import type { UserRole, WorkflowStatus } from '../types'

type Action = 'approve' | 'reject' | 'request_changes'

const props = defineProps<{
  proposalId: string
  revisionId: string
  actorRole: UserRole
  status: WorkflowStatus
  pending: boolean
}>()
const emit = defineEmits<{ success: [] }>()

const busy = ref(false)
const dialogAction = ref<Exclude<Action, 'approve'> | null>(null)
const comment = ref('')
const statusText = ref('')
const errorAction = ref<Action | null>(null)
const approveKey = ref<string | null>(null)
const rejectKey = ref<string | null>(null)
const requestChangesKey = ref<string | null>(null)
const rejectComment = ref('')
const requestChangesComment = ref('')
const rejectTrigger = ref<HTMLButtonElement | null>(null)
const requestChangesTrigger = ref<HTMLButtonElement | null>(null)
const commentInput = ref<HTMLTextAreaElement | null>(null)

const allowed = computed(() => canApprove(props.actorRole, props.status))
const locked = computed(() => props.pending || busy.value || dialogAction.value !== null)

function keyFor(action: Action): string {
  const target =
    action === 'approve' ? approveKey : action === 'reject' ? rejectKey : requestChangesKey
  if (!target.value) target.value = globalThis.crypto.randomUUID()
  return target.value
}

function clearKey(action: Action): void {
  if (action === 'approve') approveKey.value = null
  else if (action === 'reject') rejectKey.value = null
  else requestChangesKey.value = null
}

function closeDialog(): void {
  const trigger = dialogAction.value === 'reject' ? rejectTrigger.value : requestChangesTrigger.value
  dialogAction.value = null
  comment.value = ''
  void nextTick(() => trigger?.focus())
}

async function perform(action: Action, value = ''): Promise<void> {
  if (busy.value || props.pending) return
  busy.value = true
  statusText.value = '正在提交审批动作'
  errorAction.value = null
  if (action === 'reject') rejectComment.value = value
  if (action === 'request_changes') requestChangesComment.value = value
  try {
    if (action === 'approve') {
      await approveProposal(props.proposalId, props.revisionId, keyFor(action))
    } else if (action === 'reject') {
      await rejectProposal(props.proposalId, props.revisionId, value, keyFor(action))
    } else {
      await requestProposalChanges(props.proposalId, props.revisionId, value, keyFor(action))
    }
    clearKey(action)
    statusText.value = '审批动作已完成'
    if (dialogAction.value) closeDialog()
    emit('success')
  } catch {
    errorAction.value = action
    statusText.value = '审批动作失败，请使用同一请求重试'
    if (dialogAction.value) closeDialog()
  } finally {
    busy.value = false
  }
}

async function confirmApproval(): Promise<void> {
  if (locked.value || !allowed.value) return
  busy.value = true
  statusText.value = '等待确认批准'
  try {
    await ElMessageBox.confirm('确认批准并执行本地模拟发布？', '确认批准', {
      confirmButtonText: '批准',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    statusText.value = '已取消批准'
    busy.value = false
    return
  }
  busy.value = false
  await perform('approve')
}

function openCommentDialog(action: Exclude<Action, 'approve'>): void {
  if (locked.value || !allowed.value) return
  clearKey(action)
  if (action === 'reject') rejectComment.value = ''
  else requestChangesComment.value = ''
  if (errorAction.value === action) errorAction.value = null
  dialogAction.value = action
  comment.value = ''
  statusText.value = action === 'reject' ? '请输入驳回意见' : '请输入修改意见'
  void nextTick(() => commentInput.value?.focus())
}

function confirmComment(): void {
  if (!dialogAction.value) return
  const normalized = comment.value.trim()
  if (normalized.length < 1 || normalized.length > 500 || /\p{C}/u.test(normalized)) {
    statusText.value = '意见须为 1 至 500 字且不能包含控制字符'
    return
  }
  void perform(dialogAction.value, normalized)
}

function retry(): void {
  if (!errorAction.value) return
  const action = errorAction.value
  const value = action === 'reject' ? rejectComment.value : requestChangesComment.value
  void perform(action, value)
}
</script>

<template>
  <section class="approval-actions mobile-action-dock" aria-label="审批操作">
    <p v-if="!allowed" data-test="approval-disabled-reason">当前用户无审批权限。</p>
    <p v-else-if="pending" data-test="approval-disabled-reason">已有审批动作正在处理。</p>
    <div v-if="allowed" class="action-buttons">
      <button data-test="approve-action" type="button" :disabled="locked" @click="confirmApproval">
        批准并模拟发布
      </button>
      <button
        ref="rejectTrigger"
        data-test="reject-action"
        type="button"
        :disabled="locked"
        @click="openCommentDialog('reject')"
      >驳回</button>
      <button
        ref="requestChangesTrigger"
        data-test="request-changes-action"
        type="button"
        :disabled="locked"
        @click="openCommentDialog('request_changes')"
      >要求修改</button>
    </div>

    <div v-if="dialogAction" class="comment-dialog" role="dialog" aria-modal="true" aria-labelledby="comment-title">
      <h2 id="comment-title">{{ dialogAction === 'reject' ? '驳回意见' : '修改意见' }}</h2>
      <label for="approval-comment">意见（1 至 500 字）</label>
      <textarea
        id="approval-comment"
        ref="commentInput"
        v-model="comment"
        data-test="approval-comment"
        maxlength="500"
      />
      <div class="action-buttons">
        <button data-test="confirm-comment" type="button" :disabled="busy" @click="confirmComment">
          确认
        </button>
        <button data-test="cancel-comment" type="button" :disabled="busy" @click="closeDialog">
          取消
        </button>
      </div>
    </div>

    <button v-if="errorAction" :data-test="`retry-${errorAction.replace('_', '-')}`" type="button" :disabled="locked" @click="retry">
      使用同一请求重试
    </button>
    <p aria-live="polite">{{ statusText }}</p>
  </section>
</template>

<style scoped>
.approval-actions,
.comment-dialog {
  display: grid;
  gap: 12px;
}

.approval-actions {
  margin-top: 20px;
}

.action-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

button,
textarea {
  font: inherit;
}

button {
  min-height: 40px;
  padding: 8px 14px;
}

.comment-dialog {
  padding: 18px;
  border: 1px solid #94a3b8;
  background: var(--app-surface);
}

.comment-dialog h2 {
  margin: 0;
  font-size: 18px;
}

textarea {
  min-height: 96px;
}
</style>
