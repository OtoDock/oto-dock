// An action card in the history and the live state: the row and the frame
// carry the card's id, the words, the buttons' ids, labels and kinds and
// the options, never a parameter, and both rebuild the same block inside
// the assistant's message.
import { describe, it, expect } from 'vitest'

import { dbMessagesToDisplay, eventToBlock, liveBlockToMessageBlock } from '@/lib/messageBlocks'
import type { DbMessageRow, LiveBlock, PersistedEvent } from '@/api/wireEvents'

const EVENT = {
  type: 'action_card', card_id: 'card-1', title: 'Move Alpha to Eng', text: 'Places Alpha at the Team level of Eng.',
  note: 'I suggest the Team level.', buttons: [{ id: 'go', label: 'Move', kind: 'do' }, { id: 'no', label: 'Not now', kind: 'say' }],
  options: [{ id: 'delegation_edge', label: 'Let Alpha hand work to it', default: true }],
} as const

function row(role: string, content: string, eventType = '', eventData?: object): DbMessageRow {
  return { id: 1, role, content, event_type: eventType, event_data: eventData ? JSON.stringify(eventData) : '', created_at: '2026-10-28T10:00:00Z' } as DbMessageRow
}

const WANT = {
  type: 'card', cardId: 'card-1', title: 'Move Alpha to Eng', text: 'Places Alpha at the Team level of Eng.',
  note: 'I suggest the Team level.', buttons: [{ id: 'go', label: 'Move', kind: 'do' }, { id: 'no', label: 'Not now', kind: 'say' }],
  options: [{ id: 'delegation_edge', label: 'Let Alpha hand work to it', default: true }],
}

describe('an action card block', () => {
  it('rebuilds from a persisted row and from the live state alike', () => {
    const fromRow = eventToBlock(EVENT as unknown as PersistedEvent)
    const fromLive = liveBlockToMessageBlock(EVENT as unknown as LiveBlock)
    expect(fromRow).toEqual(WANT)
    expect(fromLive).toEqual(WANT)
    expect(JSON.stringify(fromRow)).not.toContain('params')
  })

  it('joins the assistant message in the history', () => {
    const messages = dbMessagesToDisplay([
      row('user', 'Move alpha to engineering.'),
      row('event', '', 'tool', { type: 'tool', name: 'mcp__agent-creator-mcp__propose_move', tool_id: 't1', active: false }),
      row('event', '', 'action_card', EVENT),
      row('assistant', 'The card is in the chat.'),
    ], [])
    const blocks = messages.flatMap((m) => m.blocks)
    const card = blocks.find((b) => b.type === 'card')
    expect(card).toEqual(WANT)
    const host = messages.find((m) => m.blocks.includes(card!))
    expect(host?.role).toBe('assistant')
  })

  it('reads a row with no note and no options', () => {
    const block = eventToBlock({ type: 'action_card', card_id: 'card-2', title: 'T', text: '', buttons: [] } as unknown as PersistedEvent)
    expect(block).toEqual({ type: 'card', cardId: 'card-2', title: 'T', text: '', note: '', buttons: [], options: [] })
  })
})
