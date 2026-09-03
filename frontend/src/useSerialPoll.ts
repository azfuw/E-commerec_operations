import { onBeforeUnmount, onMounted } from 'vue'

export function useSerialPoll<T>(
  load: (signal: AbortSignal) => Promise<T>,
  isTerminal: (value: T) => boolean,
  onValue: (value: T) => void,
): void {
  let controller: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let failures = 0
  let stopped = false

  function schedule(delay: number): void {
    if (stopped) return
    timer = setTimeout(() => void run(), delay)
  }

  async function run(): Promise<void> {
    if (stopped) return
    controller = new AbortController()
    try {
      const value = await load(controller.signal)
      if (stopped) return
      failures = 0
      onValue(value)
      if (stopped) return
      if (!isTerminal(value)) schedule(2_000)
    } catch {
      if (stopped || controller.signal.aborted) return
      failures += 1
      schedule(failures >= 2 ? 5_000 : 2_000)
    }
  }

  onMounted(() => void run())
  onBeforeUnmount(() => {
    stopped = true
    controller?.abort()
    if (timer !== null) clearTimeout(timer)
  })
}
