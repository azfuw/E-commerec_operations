import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  approveProposal: vi.fn(),
  rejectProposal: vi.fn(),
  requestProposalChanges: vi.fn(),
}))
vi.mock('../api', () => api)

import ApprovalActions from './ApprovalActions.vue'

function mountActions(role: 'operator' | 'supervisor' | 'admin' = 'supervisor') {
  return mount(ApprovalActions, {
    props: {
      proposalId: 'proposal-1',
      revisionId: 'revision-2',
      actorRole: role,
      status: 'pending_approval',
      pending: false,
    },
    attachTo: document.body,
    global: { plugins: [ElementPlus] },
  })
}

beforeEach(() => {
  api.approveProposal.mockReset()
  api.rejectProposal.mockReset()
  api.requestProposalChanges.mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('ApprovalActions', () => {
  it('keeps operator actions unavailable with a visible reason', () => {
    const wrapper = mountActions('operator')
    expect(wrapper.find('[data-test="approve-action"]').exists()).toBe(false)
    expect(wrapper.get('[data-test="approval-disabled-reason"]').text()).toContain('无审批权限')
    wrapper.unmount()
  })

  it('locks all actions while confirming and retries approval with one key', async () => {
    let finishConfirmation!: (value: string) => void
    vi.spyOn(ElMessageBox, 'confirm').mockImplementation(
      () => new Promise((resolve) => (finishConfirmation = resolve)),
    )
    api.approveProposal.mockRejectedValueOnce(new Error('failed')).mockResolvedValueOnce({})
    const wrapper = mountActions()
    const approve = wrapper.get('[data-test="approve-action"]')

    await approve.trigger('click')
    await approve.trigger('click')
    expect(ElMessageBox.confirm).toHaveBeenCalledOnce()
    expect(wrapper.get('[aria-live="polite"]').text()).toContain('等待确认')
    expect(wrapper.get('[data-test="reject-action"]').attributes('disabled')).toBeDefined()

    finishConfirmation('confirm')
    await flushPromises()
    await wrapper.get('[data-test="retry-approve"]').trigger('click')
    await flushPromises()

    expect(api.approveProposal).toHaveBeenCalledTimes(2)
    expect(api.approveProposal).toHaveBeenNthCalledWith(
      1,
      'proposal-1',
      'revision-2',
      expect.any(String),
    )
    expect(api.approveProposal.mock.calls[1]![2]).toBe(api.approveProposal.mock.calls[0]![2])
    expect(wrapper.emitted('success')).toHaveLength(1)
    wrapper.unmount()
  })

  it('validates trimmed comments and never reuses keys across actions', async () => {
    api.rejectProposal.mockRejectedValueOnce(new Error('failed')).mockResolvedValueOnce({})
    api.requestProposalChanges
      .mockRejectedValueOnce(new Error('failed'))
      .mockResolvedValueOnce({})
    const wrapper = mountActions()

    await wrapper.get('[data-test="reject-action"]').trigger('click')
    await wrapper.get('[data-test="approval-comment"]').setValue('  驳回原因  ')
    await wrapper.get('[data-test="confirm-comment"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="retry-reject"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="request-changes-action"]').trigger('click')
    await wrapper.get('[data-test="approval-comment"]').setValue('   ')
    await wrapper.get('[data-test="confirm-comment"]').trigger('click')
    expect(api.requestProposalChanges).not.toHaveBeenCalled()
    expect(wrapper.get('[aria-live="polite"]').text()).toContain('1 至 500')
    await wrapper.get('[data-test="approval-comment"]').setValue('修改标题')
    await wrapper.get('[data-test="confirm-comment"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="retry-request-changes"]').trigger('click')
    await flushPromises()

    expect(api.rejectProposal).toHaveBeenCalledTimes(2)
    expect(api.requestProposalChanges).toHaveBeenCalledTimes(2)
    expect(api.rejectProposal).toHaveBeenNthCalledWith(
      1,
      'proposal-1',
      'revision-2',
      '驳回原因',
      expect.any(String),
    )
    expect(api.requestProposalChanges).toHaveBeenNthCalledWith(
      1,
      'proposal-1',
      'revision-2',
      '修改标题',
      expect.any(String),
    )
    expect(api.rejectProposal.mock.calls[0]![3]).not.toBe(
      api.requestProposalChanges.mock.calls[0]![3],
    )
    expect(api.rejectProposal.mock.calls[1]![3]).toBe(api.rejectProposal.mock.calls[0]![3])
    expect(api.requestProposalChanges.mock.calls[1]![3]).toBe(
      api.requestProposalChanges.mock.calls[0]![3],
    )
    wrapper.unmount()
  })

  it.each([
    ['reject', 'reject-action', 'retry-reject', api.rejectProposal],
    [
      'request changes',
      'request-changes-action',
      'retry-request-changes',
      api.requestProposalChanges,
    ],
  ] as const)('uses a fresh key after reopening failed %s with a new comment', async (
    _name,
    actionSelector,
    retrySelector,
    request,
  ) => {
    request.mockRejectedValueOnce(new Error('failed')).mockRejectedValueOnce(new Error('failed'))
    request.mockResolvedValueOnce({})
    const wrapper = mountActions()

    await wrapper.get(`[data-test="${actionSelector}"]`).trigger('click')
    await wrapper.get('[data-test="approval-comment"]').setValue('意见 A')
    await wrapper.get('[data-test="confirm-comment"]').trigger('click')
    await flushPromises()
    await wrapper.get(`[data-test="${retrySelector}"]`).trigger('click')
    await flushPromises()
    expect(request.mock.calls[1]![3]).toBe(request.mock.calls[0]![3])
    expect(request.mock.calls[1]![2]).toBe('意见 A')

    await wrapper.get(`[data-test="${actionSelector}"]`).trigger('click')
    await wrapper.get('[data-test="approval-comment"]').setValue('意见 B')
    await wrapper.get('[data-test="confirm-comment"]').trigger('click')
    await flushPromises()

    expect(request).toHaveBeenCalledTimes(3)
    expect(request.mock.calls[2]![2]).toBe('意见 B')
    expect(request.mock.calls[2]![3]).not.toBe(request.mock.calls[0]![3])
    wrapper.unmount()
  })

  it('rejects control characters and returns focus when a dialog closes', async () => {
    const wrapper = mountActions()
    const trigger = wrapper.get('[data-test="request-changes-action"]')
    ;(trigger.element as HTMLElement).focus()
    await trigger.trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(wrapper.get('[data-test="approval-comment"]').element)
    await wrapper.get('[data-test="approval-comment"]').setValue('修改\u0000原因')
    await wrapper.get('[data-test="confirm-comment"]').trigger('click')
    expect(api.requestProposalChanges).not.toHaveBeenCalled()
    await wrapper.get('[data-test="cancel-comment"]').trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })
})
