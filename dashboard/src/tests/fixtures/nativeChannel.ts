// A stand-in for the Android app's origin-scoped `OtoDockNative` object: it
// records every post (header fields plus the optional body after the first
// newline) and answers the replying methods from `answers`.

export interface NativePost {
  m: string
  a: unknown[]
  id?: number
  body?: string
}

export interface FakeNativeChannel {
  posted: NativePost[]
  methods(): string[]
  /** Answer an ask by hand (for answers the test decides later). */
  reply(id: number, r: unknown): void
  uninstall(): void
}

export function installFakeNativeChannel(answers: Record<string, unknown> = {}): FakeNativeChannel {
  const listeners: Array<(event: { data: unknown }) => void> = []
  const posted: NativePost[] = []
  const send = (id: number, r: unknown) => {
    for (const l of listeners) l({ data: JSON.stringify({ id, r }) })
  }
  const channel = {
    postMessage(message: string) {
      const nl = message.indexOf('\n')
      const head = JSON.parse(nl < 0 ? message : message.slice(0, nl)) as NativePost
      posted.push(nl < 0 ? head : { ...head, body: message.slice(nl + 1) })
      if (head.id && Object.prototype.hasOwnProperty.call(answers, head.m)) {
        const id = head.id
        queueMicrotask(() => send(id, answers[head.m]))
      }
    },
    addEventListener(type: string, listener: (event: { data: unknown }) => void) {
      if (type === 'message') listeners.push(listener)
    },
  }
  ;(window as unknown as { OtoDockNative?: unknown }).OtoDockNative = channel
  return {
    posted,
    methods: () => posted.map((p) => p.m),
    reply: send,
    uninstall: () => { delete (window as unknown as { OtoDockNative?: unknown }).OtoDockNative },
  }
}

/** Counts every read of `window.Android` (the retired raw bridge name). */
export function watchRetiredAndroidName(): { reads: () => number; restore: () => void } {
  let reads = 0
  Object.defineProperty(window, 'Android', {
    configurable: true,
    get() { reads++; return undefined },
  })
  return {
    reads: () => reads,
    restore: () => { delete (window as unknown as { Android?: unknown }).Android },
  }
}
