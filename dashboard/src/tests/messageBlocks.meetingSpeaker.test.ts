/**
 * Meeting speaker badge on event blocks after a mid-turn user message.
 *
 * A user message that lands mid-speaker-turn splits the turn; when the next
 * persisted row is an EVENT (tool card / artifact) there is no
 * meeting_turn_start to open the new assistant group. The pump stamps
 * `_meeting_agent` (slug) on every persisted meeting block — the history
 * rebuild resolves it against the agents list so the card keeps its badge.
 */
import { describe, it, expect } from 'vitest'
import { dbMessagesToDisplay } from '@/lib/messageBlocks'

const AGENTS = [
  { name: 'writer', display_name: 'Writer', color: '#673a97' },
]

let nextId = 1
function row(role: string, content: string, eventType = '', eventData: object | null = null) {
  return {
    id: nextId++,
    role,
    content,
    event_type: eventType,
    event_data: eventData ? JSON.stringify(eventData) : '',
    created_at: '2026-07-07T00:00:00+00:00',
  }
}

describe('meeting speaker identity on event rows', () => {
  it('resolves _meeting_agent when an event opens a new assistant group', () => {
    const msgs = dbMessagesToDisplay(
      [
        row('user', 'hold on, one question'),
        row('event', '', 'tool', {
          type: 'tool', name: 'Bash', summary: 'ls', _meeting_agent: 'writer',
        }),
      ],
      AGENTS,
    )
    const group = msgs[msgs.length - 1]
    expect(group.role).toBe('assistant')
    expect(group.agentSlug).toBe('writer')
    expect(group.agentDisplayName).toBe('Writer')
    expect(group.agentColor).toBe('#673a97')
    expect(group.badge).toBe('meeting')
    expect(group.blocks.some((b) => b.type === 'tool')).toBe(true)
  })

  it('leaves non-meeting event groups identity-less', () => {
    const msgs = dbMessagesToDisplay(
      [
        row('user', 'run it'),
        row('event', '', 'tool', { type: 'tool', name: 'Bash', summary: 'ls' }),
      ],
      AGENTS,
    )
    const group = msgs[msgs.length - 1]
    expect(group.role).toBe('assistant')
    expect(group.agentSlug).toBeUndefined()
    expect(group.badge).toBeUndefined()
  })

  it('meeting_turn_start identity still wins over the block stamp', () => {
    const msgs = dbMessagesToDisplay(
      [
        row('event', '', 'system', {
          subtype: 'meeting_turn_start', agent: 'writer',
          agent_display_name: 'Writer', agent_color: '#673a97',
        }),
        row('event', '', 'tool', {
          type: 'tool', name: 'Bash', summary: 'ls', _meeting_agent: 'writer',
        }),
      ],
      AGENTS,
    )
    const group = msgs[msgs.length - 1]
    expect(group.agentSlug).toBe('writer')
    expect(group.badge).toBe('meeting')
  })

  it('meeting_failed keeps its reason through the history rebuild', () => {
    // Pre-turn meeting failure (admission denial) — the persisted banner must
    // still carry the orchestrator's reason after a page reload.
    const msgs = dbMessagesToDisplay(
      [
        row('event', '', 'system', {
          type: 'system',
          subtype: 'meeting_failed',
          message: 'The platform host is low on memory.',
        }),
      ],
      AGENTS,
    )
    const group = msgs[msgs.length - 1]
    const block = group.blocks.find(
      (b) => b.type === 'system' && (b as any).subtype === 'meeting_failed',
    ) as any
    expect(block).toBeTruthy()
    expect(block.message).toBe('The platform host is low on memory.')
  })
})
