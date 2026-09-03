import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useSerialPoll } from './useSerialPoll'

function pollHarness<T>(
  load: (signal: AbortSignal) => Promise<T>,
  isTerminal: (value: T) => boolean = () => false,
  onValue: (value: T) => void = () => undefined,
) {
  return mount(
    defineComponent({
      setup() {
        useSerialPoll(load, isTerminal, onValue)
        return () => h('div')
      },
    }),
  )
}

afterEach(() => {
  vi.useRealTimers()
})

describe('useSerialPoll', () => {
  it('never overlaps requests and waits two seconds after success', async () => {
    vi.useFakeTimers()
    const finishes: ((value: { status: string }) => void)[] = []
    let calls = 0
    const wrapper = pollHarness<{ status: string }>(
      () => {
        calls += 1
        return new Promise((resolve) => finishes.push(resolve))
      },
    )
    await flushPromises()

    await vi.advanceTimersByTimeAsync(10_000)
    expect(calls).toBe(1)

    finishes[0]?.({ status: 'processing' })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1_999)
    expect(calls).toBe(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toBe(2)
    wrapper.unmount()
  })

  it('backs off to five seconds after two failures and resets after success', async () => {
    vi.useFakeTimers()
    const outcomes = [
      () => Promise.reject(new Error('offline-1')),
      () => Promise.reject(new Error('offline-2')),
      () => Promise.resolve({ status: 'processing' }),
      () => Promise.resolve({ status: 'processing' }),
    ]
    let calls = 0
    const wrapper = pollHarness(() => outcomes[calls++]!())
    await flushPromises()

    await vi.advanceTimersByTimeAsync(2_000)
    expect(calls).toBe(2)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(4_999)
    expect(calls).toBe(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toBe(3)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1_999)
    expect(calls).toBe(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toBe(4)
    wrapper.unmount()
  })

  it('stops after a terminal value', async () => {
    vi.useFakeTimers()
    let calls = 0
    const values: string[] = []
    const wrapper = pollHarness(
      async () => {
        calls += 1
        return { status: 'completed' }
      },
      (value) => value.status === 'completed',
      (value) => values.push(value.status),
    )
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(calls).toBe(1)
    expect(values).toEqual(['completed'])
    wrapper.unmount()
  })

  it('does not schedule when onValue unmounts the component', async () => {
    vi.useFakeTimers()
    let calls = 0
    let wrapper: ReturnType<typeof pollHarness<{ status: string }>>
    wrapper = pollHarness(
      async () => {
        calls += 1
        return { status: 'processing' }
      },
      () => false,
      () => wrapper.unmount(),
    )
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2_000)

    expect(calls).toBe(1)
  })

  it('aborts the active request and stops on unmount', async () => {
    vi.useFakeTimers()
    let activeSignal: AbortSignal | undefined
    let calls = 0
    const wrapper = pollHarness((signal) => {
      calls += 1
      activeSignal = signal
      return new Promise(() => undefined)
    })
    await flushPromises()

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(activeSignal?.aborted).toBe(true)
    expect(calls).toBe(1)
  })
})
