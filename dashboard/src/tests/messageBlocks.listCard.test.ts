// A list card in the history and the live state: the row and the frame
// carry the kind, the filters, the title and the person, never a row of the
// list, and both rebuild the same block inside the assistant's message. A
// person's row with a card's mark in its event_data draws the caption.
import { describe, it, expect } from 'vitest'

import { dbMessagesToDisplay, eventToBlock, liveBlockToMessageBlock } from '@/lib/messageBlocks'
import type { DbMessageRow, LiveBlock, PersistedEvent } from '@/api/wireEvents'

const EVENT = {
  type: 'list_card', kind: 'tasks', filters: { scope: 'mine', status: 'active' }, title: 'Your scheduled tasks', person: 'u1',
} as const

function row(id: number, role: string, content: string, eventType = '', eventData?: object): DbMessageRow {
  return { id, role, content, event_type: eventType, event_data: eventData ? JSON.stringify(eventData) : '', created_at: '2026-10-28T10:00:00Z' } as DbMessageRow
}

const WANT = { type: 'list', kind: 'tasks', filters: { scope: 'mine', status: 'active' }, title: 'Your scheduled tasks', person: 'u1' }

describe('a list card block', () => {
  it('rebuilds from a persisted row and from the live state alike, with no row of the list', () => {
    const fromRow = eventToBlock(EVENT as unknown as PersistedEvent)
    const fromLive = liveBlockToMessageBlock(EVENT as unknown as LiveBlock)
    expect(fromRow).toEqual(WANT)
    expect(fromLive).toEqual(WANT)
    expect(Object.keys(fromRow!).sort()).toEqual(['filters', 'kind', 'person', 'title', 'type'])
  })

  it('joins the assistant message in the history', () => {
    const messages = dbMessagesToDisplay([
      row(1, 'user', 'What are my schedules?'),
      row(2, 'event', '', 'list_card', EVENT),
      row(3, 'assistant', 'Here they are.'),
    ], [])
    const blocks = messages.flatMap((m) => m.blocks)
    const list = blocks.find((b) => b.type === 'list')
    expect(list).toEqual(WANT)
    expect(messages.find((m) => m.blocks.includes(list!))?.role).toBe('assistant')
  })

  it('reads a row with no filters', () => {
    const block = eventToBlock({ type: 'list_card', kind: 'machines', title: 'Remote machines', person: 'u1' } as unknown as PersistedEvent)
    expect(block).toEqual({ type: 'list', kind: 'machines', filters: {}, title: 'Remote machines', person: 'u1' })
  })

  it("marks a person's row a card's say button sent", () => {
    const [typed, sent] = dbMessagesToDisplay([
      row(1, 'user', 'Move it.'),
      row(2, 'user', 'Not now, thanks.', '', { card: { card_id: 'card-1', button: 'no' } }),
    ], [])
    expect(typed.fromCard).toBeUndefined()
    expect(sent.fromCard).toBe(true)
    expect(sent.blocks).toEqual([{ type: 'text', content: 'Not now, thanks.' }])
  })
})
