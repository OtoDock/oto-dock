import { describe, it, expect, beforeEach } from 'vitest'
import { useChatStore, newChatKey, NEW_CHAT_PREFIX } from '../store/chatStore'

// Sidebar live-dot truth through the warmup lifecycle. Visiting a MID-TURN
// interactive chat re-fires warmup_started/warmup_ready (idempotent re-warm);
// status broadcasts are transition-only, so a clobber here kills the dot for
// good — these pin the no-downgrade guards + the warmup_ready turn_open
// reconciliation.

const CID = 'chat-warmup-test'
const AGENT = 'agent-transfer-test'
const MINTED = 'chat-minted-test'

beforeEach(() => {
  useChatStore.setState((s) => {
    const byChat = { ...s.byChat }
    delete byChat[CID]
    delete byChat[MINTED]
    for (const key of Object.keys(byChat)) {
      if (key.startsWith(NEW_CHAT_PREFIX)) delete byChat[key]
    }
    return { byChat }
  })
})

describe('chatStore warmup transitions vs the streaming dot', () => {
  it('beginWarmup never downgrades a streaming slice', () => {
    const st = useChatStore.getState()
    st.setStreaming(CID)
    st.beginWarmup(CID, { agent: 'a' })
    expect(useChatStore.getState().byChat[CID].status).toBe('streaming')
  })

  it('beginWarmup still marks an idle slice as warming', () => {
    const st = useChatStore.getState()
    st.setReady(CID) // no slice yet → noop; create via beginWarmup below
    st.beginWarmup(CID, { agent: 'a' })
    expect(useChatStore.getState().byChat[CID].status).toBe('warming')
  })

  it('visit sequence begin→finish keeps a mid-turn chat streaming', () => {
    const st = useChatStore.getState()
    st.setStreaming(CID)
    st.beginWarmup(CID, { agent: 'a' })
    st.finishWarmup(CID, {})
    expect(useChatStore.getState().byChat[CID].status).toBe('streaming')
  })

  it('warmup_ready turn_open=true lights a dot this client never saw start', () => {
    const st = useChatStore.getState()
    st.beginWarmup(CID, { agent: 'a' })
    st.finishWarmup(CID, { turn_open: true })
    expect(useChatStore.getState().byChat[CID].status).toBe('streaming')
  })

  it('warmup_ready turn_open=false clears a stale streaming slice', () => {
    const st = useChatStore.getState()
    st.setStreaming(CID)
    st.beginWarmup(CID, { agent: 'a' })
    st.finishWarmup(CID, { turn_open: false })
    expect(useChatStore.getState().byChat[CID].status).toBe('ready')
  })

  it('warmup_ready without turn_open keeps the resend-path guard', () => {
    const st = useChatStore.getState()
    st.setStreaming(CID)
    st.finishWarmup(CID, {})
    expect(useChatStore.getState().byChat[CID].status).toBe('streaming')
  })
})

// The new-chat page keeps its draft under `__new__:<agent>` until the warmup
// mints the chat id; the dispatcher then moves the slice (only for a frame
// that says new_chat, only on the new-chat page, once — useDashboardWs).
describe('chatStore transferNewChatToChat', () => {
  it('moves the draft, the queue and the attachments onto the minted id', () => {
    const st = useChatStore.getState()
    st.setDraftInput(newChatKey(AGENT), 'typed before the id existed')
    st.setQueuedMessages(newChatKey(AGENT), [{ text: 'queued' }])
    st.setPendingImages(newChatKey(AGENT), [{ id: 'i1', base64: '', name: 'a.png' }])
    st.transferNewChatToChat(AGENT, MINTED)
    const byChat = useChatStore.getState().byChat
    expect(byChat[newChatKey(AGENT)]).toBeUndefined()
    expect(byChat[MINTED].draftInput).toBe('typed before the id existed')
    expect(byChat[MINTED].queuedMessages).toEqual([{ text: 'queued' }])
    expect(byChat[MINTED].pendingImages.map((i) => i.id)).toEqual(['i1'])
  })

  it('never overwrites a draft already under the target id', () => {
    const st = useChatStore.getState()
    st.setDraftInput(MINTED, 'already here')
    st.setDraftInput(newChatKey(AGENT), 'late arrival')
    st.transferNewChatToChat(AGENT, MINTED)
    const byChat = useChatStore.getState().byChat
    expect(byChat[MINTED].draftInput).toBe('already here')
    expect(byChat[newChatKey(AGENT)]).toBeUndefined()
  })

  it('is a no-op when the new-chat slice carries nothing', () => {
    const st = useChatStore.getState()
    st.setDraftInput(newChatKey(AGENT), '')
    st.transferNewChatToChat(AGENT, MINTED)
    expect(useChatStore.getState().byChat[MINTED]).toBeUndefined()
  })
})
