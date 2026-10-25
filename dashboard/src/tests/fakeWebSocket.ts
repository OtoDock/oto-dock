// Shared WebSocket stand-in for the socket hook tests (useDashboardWs,
// useDuplexVoice, useActiveChats, the audio routing): it records what the
// hook sends and lets the test drive the socket's handlers. A test stubs the
// global with it: `vi.stubGlobal('WebSocket', FakeWebSocket as unknown as
// typeof WebSocket)`, and resets `FakeWebSocket.instances` in beforeEach.

export interface FakeWebSocketOptions {
  /** false: the socket stays CONNECTING and close() marks it CLOSED. */
  opens?: boolean
}

/** A fresh fake socket class; `Sent` is what the hook writes (a duplex
 * socket also sends binary audio). */
export function fakeWebSocketClass<Sent = string>({ opens = true }: FakeWebSocketOptions = {}) {
  return class FakeWebSocket {
    static CONNECTING = 0
    static OPEN = 1
    static CLOSING = 2
    static CLOSED = 3
    static instances: FakeWebSocket[] = []
    readyState = opens ? FakeWebSocket.OPEN : FakeWebSocket.CONNECTING
    binaryType = ''
    sent: Sent[] = []
    onopen: (() => void) | null = null
    onclose: ((e?: { code: number; reason: string }) => void) | null = null
    onerror: (() => void) | null = null
    onmessage: ((e: { data: string | ArrayBuffer }) => void) | null = null
    constructor(public url: string) {
      FakeWebSocket.instances.push(this)
    }
    send(data: Sent) { this.sent.push(data) }
    // Never fires onclose: the test drives it, so no reconnect runs.
    close() { if (!opens) this.readyState = FakeWebSocket.CLOSED }
    addEventListener() { /* noop */ }
    removeEventListener() { /* noop */ }
  }
}

export const FakeWebSocket = fakeWebSocketClass()
export type FakeWebSocket = InstanceType<typeof FakeWebSocket>
