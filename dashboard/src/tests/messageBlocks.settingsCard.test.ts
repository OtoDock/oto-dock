// A setting's card in the history and the live state: the row and the
// frame carry the id, the label and the catalog's version, never a value,
// and both rebuild the same block inside the assistant's message.
import { describe, it, expect } from 'vitest'

import { dbMessagesToDisplay, eventToBlock, liveBlockToMessageBlock } from '@/lib/messageBlocks'
import type { DbMessageRow, LiveBlock, PersistedEvent } from '@/api/wireEvents'

const EVENT = { type: 'settings_card', setting: 'user.general.wake-word', label: 'Wake word', catalog_version: 1 } as const

function row(role: string, content: string, eventType = '', eventData?: object): DbMessageRow {
  return { id: 1, role, content, event_type: eventType, event_data: eventData ? JSON.stringify(eventData) : '', created_at: '2026-10-27T10:00:00Z' } as DbMessageRow
}

describe('a setting card block', () => {
  it('rebuilds from a persisted row and from the live state alike', () => {
    const fromRow = eventToBlock(EVENT as unknown as PersistedEvent)
    const fromLive = liveBlockToMessageBlock(EVENT as unknown as LiveBlock)
    const want = { type: 'setting', settingId: 'user.general.wake-word', label: 'Wake word', catalogVersion: 1 }
    expect(fromRow).toEqual(want)
    expect(fromLive).toEqual(want)
    expect(JSON.stringify(fromRow)).not.toContain('value')
  })

  it('joins the assistant message in the history', () => {
    const messages = dbMessagesToDisplay([
      row('user', 'Where is the wake word?'),
      row('event', '', 'tool', { type: 'tool', name: 'mcp__settings-mcp__get_setting', tool_id: 't1', active: false }),
      row('event', '', 'settings_card', EVENT),
      row('assistant', 'The card is in the chat.'),
    ], [])
    const blocks = messages.flatMap((m) => m.blocks)
    const card = blocks.find((b) => b.type === 'setting')
    expect(card).toEqual({ type: 'setting', settingId: 'user.general.wake-word', label: 'Wake word', catalogVersion: 1 })
    const host = messages.find((m) => m.blocks.includes(card!))
    expect(host?.role).toBe('assistant')
  })
})
