/**
 * The new-chat draft moves onto a minted chat id at warmup_started — and only
 * then. The same frame fires for an existing chat re-warmed by a send (the
 * draft typed in a new chat used to jump into that chat), only the new-chat
 * page holds a draft that may move, and a reconnect replays a warming chat's
 * frame with the flag still on it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { useChatStore, newChatKey } from '@/store/chatStore'

class FakeWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static instances: FakeWebSocket[] = []
  readyState = FakeWebSocket.OPEN
  sent: string[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  send(data: string) { this.sent.push(data) }
  close() { /* keep reconnect logic out of the test */ }
}

const AGENT = 'agent-x'
const MINTED = 'chat-minted'

function seedDraft(text: string) {
  useChatStore.getState().setDraftInput(newChatKey(AGENT), text)
}
function draftOf(key: string): string {
  return useChatStore.getState().byChat[key]?.draftInput ?? ''
}
function hasSlice(key: string): boolean {
  return key in useChatStore.getState().byChat
}

describe('useDashboardWs new-chat draft transfer at warmup_started', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    useChatStore.setState({ byChat: {} })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function connect(viewedChatId: string | null | undefined) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const hook = renderHook(() => useDashboardWs({ viewedChatId }), { wrapper })
    act(() => { hook.result.current.connect() })
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })
    return { hook, ws }
  }

  function warmupStarted(ws: FakeWebSocket, newChat?: boolean) {
    const msg: Record<string, unknown> = {
      type: 'warmup_started', chat_id: MINTED, agent: AGENT,
      execution_path: 'claude-code-cli', execution_target: 'local',
    }
    if (newChat !== undefined) msg.new_chat = newChat
    act(() => { ws.onmessage?.({ data: JSON.stringify(msg) }) })
  }

  it('moves the draft when the new-chat page receives a minted chat frame', () => {
    const { ws } = connect(null)
    seedDraft('draft')
    warmupStarted(ws, true)
    expect(draftOf(MINTED)).toBe('draft')
    expect(hasSlice(newChatKey(AGENT))).toBe(false)
  })

  it('leaves the draft alone when an existing chat is re-warmed', () => {
    const { ws } = connect(null)
    seedDraft('draft')
    warmupStarted(ws, false)
    expect(draftOf(newChatKey(AGENT))).toBe('draft')
    expect(draftOf(MINTED)).toBe('')
  })

  it('leaves the draft alone when the frame carries no flag at all', () => {
    const { ws } = connect(null)
    seedDraft('draft')
    warmupStarted(ws)
    expect(draftOf(newChatKey(AGENT))).toBe('draft')
    expect(draftOf(MINTED)).toBe('')
  })

  it('leaves the draft alone on a page viewing another chat', () => {
    const { ws } = connect('chat-1')
    seedDraft('draft')
    warmupStarted(ws, true)
    expect(draftOf(newChatKey(AGENT))).toBe('draft')
    expect(draftOf(MINTED)).toBe('')
  })

  it('leaves the draft alone for a consumer that opted out of chat filtering', () => {
    const { ws } = connect(undefined)
    seedDraft('draft')
    warmupStarted(ws, true)
    expect(draftOf(newChatKey(AGENT))).toBe('draft')
    expect(draftOf(MINTED)).toBe('')
  })

  it('moves once per minted id: a replayed frame ignores a draft typed since', () => {
    const { ws } = connect(null)
    warmupStarted(ws, true)  // the send itself: nothing to carry
    seedDraft('typed since')
    warmupStarted(ws, true)  // resume_chat replay of the same frame
    expect(draftOf(newChatKey(AGENT))).toBe('typed since')
    expect(draftOf(MINTED)).toBe('')
  })
})
