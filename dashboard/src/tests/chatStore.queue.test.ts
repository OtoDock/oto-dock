/**
 * The chat's queue in the store: chips keyed by the proxy's queue id (a
 * re-announce replaces in place, the index places a new one), the index as
 * the fallback for a 1.7.0 proxy, a clear by the listed ids, and the v3
 * migration that lifts older persisted chips (the first snapshot replaces
 * them).
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { useChatStore } from '@/store/chatStore'
import { toQueuedMessage } from '@/store/types'

const chips = (cid: string) => useChatStore.getState().byChat[cid]?.queuedMessages ?? []

describe('the chat queue in the store', () => {
  beforeEach(() => { useChatStore.setState({ byChat: {} }) })

  it('adds by id at the proxy index and replaces a re-announced chip in place', () => {
    const st = useChatStore.getState()
    st.addQueuedMessage('c', 0, { text: 'a', queueId: 'qa' })
    st.addQueuedMessage('c', 1, { text: 'b', queueId: 'qb' })
    st.addQueuedMessage('c', 0, { text: 'a again', queueId: 'qa' })
    expect(chips('c').map((m) => [m.queueId, m.text])).toEqual([['qa', 'a again'], ['qb', 'b']])
  })

  it('keys a chip without an id by its index (a 1.7.0 proxy)', () => {
    const st = useChatStore.getState()
    st.addQueuedMessage('c', 1, { text: 'second' })
    expect(chips('c').map((m) => m.text)).toEqual(['', 'second'])
    st.removeQueuedMessage('c', { index: 0 })
    expect(chips('c').map((m) => m.text)).toEqual(['second'])
  })

  it('removes by id, and clears the listed ids or every chip', () => {
    const st = useChatStore.getState()
    for (const id of ['q1', 'q2', 'q3']) st.addQueuedMessage('c', 9, { text: id, queueId: id })
    st.removeQueuedMessage('c', { queueId: 'q2' })
    expect(chips('c').map((m) => m.queueId)).toEqual(['q1', 'q3'])
    st.clearQueuedMessages('c', ['q3'])
    expect(chips('c').map((m) => m.queueId)).toEqual(['q1'])
    st.clearQueuedMessages('c')
    expect(chips('c')).toEqual([])
  })

  it('reads a frame entry, a v2 item and a v1 text', () => {
    expect(toQueuedMessage({ queue_id: 'q', text: 't', author_sub: 'u', files: [{ path: 'p', name: 'n' }] }))
      .toEqual({ text: 't', queueId: 'q', authorSub: 'u', files: [{ path: 'p', name: 'n' }] })
    expect(toQueuedMessage({ text: 'v2' })).toEqual({ text: 'v2' })
    expect(toQueuedMessage('v1')).toEqual({ text: 'v1' })
  })

  it('migrates a v2 store to v3 without ids', () => {
    const persist = (useChatStore as unknown as { persist: { getOptions: () => {
      version: number; migrate: (s: unknown, v: number) => unknown } } }).persist
    const opts = persist.getOptions()
    expect(opts.version).toBe(3)
    const migrated = opts.migrate({ byChat: { c: { queuedMessages: ['old', { text: 'v2' }] } } }, 2) as {
      byChat: Record<string, { queuedMessages: unknown[] }> }
    expect(migrated.byChat.c.queuedMessages).toEqual([{ text: 'old' }, { text: 'v2' }])
  })
})
