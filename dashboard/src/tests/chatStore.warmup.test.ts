import { describe, it, expect, beforeEach } from 'vitest'
import { useChatStore } from '../store/chatStore'

// Sidebar live-dot truth through the warmup lifecycle. Visiting a MID-TURN
// interactive chat re-fires warmup_started/warmup_ready (idempotent re-warm);
// status broadcasts are transition-only, so a clobber here kills the dot for
// good — these pin the no-downgrade guards + the warmup_ready turn_open
// reconciliation.

const CID = 'chat-warmup-test'

beforeEach(() => {
  useChatStore.setState((s) => {
    const byChat = { ...s.byChat }
    delete byChat[CID]
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
