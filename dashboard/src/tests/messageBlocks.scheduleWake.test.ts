/**
 * History replay of a scheduled self-continuation: the schedule_wake row is
 * a turn boundary like a delegate result or a nudge, so the woken turn's
 * answer starts its own bubble under the wake marker instead of appending
 * to the answer before it.
 */
import { describe, it, expect } from 'vitest'
import { dbMessagesToDisplay } from '@/lib/messageBlocks'

let nextId = 1
function row(role: string, content: string, eventType = '', eventData: object | null = null) {
  return {
    id: nextId++,
    role,
    content,
    event_type: eventType,
    event_data: eventData ? JSON.stringify(eventData) : '',
    created_at: '2026-10-06T19:10:00+00:00',
  }
}

describe('schedule_wake history replay', () => {
  it('opens a new bubble with the wake marker, the answer under it', () => {
    const msgs = dbMessagesToDisplay(
      [
        row('user', 'watch the upload'),
        row('assistant', 'Started the watcher, checking back in 5 minutes.'),
        row('event', '', 'schedule_wake', { prompt: 'Check the upload log.', task_id: 'dyn-c1' }),
        row('assistant', 'The upload finished.'),
      ],
      [],
    )
    expect(msgs).toHaveLength(3)
    expect(msgs[1].blocks).toEqual([
      { type: 'text', content: 'Started the watcher, checking back in 5 minutes.' },
    ])
    expect(msgs[2].role).toBe('assistant')
    expect(msgs[2].blocks).toEqual([
      { type: 'schedulewake', prompt: 'Check the upload log.' },
      { type: 'text', content: 'The upload finished.' },
    ])
  })

  it('two wakes in a row each open their own bubble', () => {
    const msgs = dbMessagesToDisplay(
      [
        row('assistant', 'first'),
        row('event', '', 'schedule_wake', { prompt: 'one' }),
        row('assistant', 'second'),
        row('event', '', 'schedule_wake', { prompt: 'two' }),
        row('assistant', 'third'),
      ],
      [],
    )
    expect(msgs.map((m) => m.blocks.map((b) => b.type))).toEqual([
      ['text'], ['schedulewake', 'text'], ['schedulewake', 'text'],
    ])
  })
})
