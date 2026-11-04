/**
 * History replay of a scheduled self-continuation: the schedule_wake row is
 * the platform's own prompt, drawn as its own card (a wake card), and a turn
 * boundary like a delegate result, so the woken turn's answer starts its own
 * bubble under the card instead of appending to the answer before it.
 */
import { describe, it, expect } from 'vitest'
import { dbMessagesToDisplay, isAgentReply, wakeMessage } from '@/lib/messageBlocks'

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
  it('is its own card, the answer in a fresh bubble under it', () => {
    const msgs = dbMessagesToDisplay(
      [
        row('user', 'watch the upload'),
        row('assistant', 'Started the watcher, checking back in 5 minutes.'),
        row('event', '', 'schedule_wake', { prompt: 'Check the upload log.', task_id: 'dyn-c1' }),
        row('assistant', 'The upload finished.'),
      ],
      [],
    )
    expect(msgs).toHaveLength(4)
    expect(msgs[1].blocks).toEqual([
      { type: 'text', content: 'Started the watcher, checking back in 5 minutes.' },
    ])
    const wake = msgs[2]
    expect(wake.role).toBe('assistant')
    expect(wake.id).toBe(`db-evt-${msgs[2].id.replace('db-evt-', '')}`)
    expect(wake.delegateCard).toEqual({ kind: 'wake', key: expect.stringMatching(/^w:dyn-c1:/) })
    expect(wake.blocks).toEqual([{ type: 'text', content: 'Check the upload log.' }])
    // The platform's words are nobody's reply.
    expect(isAgentReply(wake)).toBe(false)
    expect(msgs[3].delegateCard).toBeUndefined()
    expect(msgs[3].blocks).toEqual([{ type: 'text', content: 'The upload finished.' }])
    expect(isAgentReply(msgs[3])).toBe(true)
  })

  it('two wakes in a row are two cards, each answer its own bubble', () => {
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
    expect(msgs.map((m) => m.delegateCard?.kind ?? m.blocks.map((b) => b.type).join())).toEqual([
      'text', 'wake', 'text', 'wake', 'text',
    ])
    // Two wakes with different prompts are two keys.
    expect(msgs[1].delegateCard?.key).not.toBe(msgs[3].delegateCard?.key)
  })

  it("the platform's resume after the agent's move keeps its source", () => {
    const msgs = dbMessagesToDisplay(
      [
        row('assistant', 'Moving to drill-sat.'),
        row('event', '', 'schedule_wake', { prompt: 'You now run on drill-sat. Continue the work.', source: 'move' }),
        row('assistant', 'Picked up on drill-sat.'),
      ],
      [],
    )
    expect(msgs[1].delegateCard).toEqual({
      kind: 'wake', key: expect.stringMatching(/^w::/), source: 'move',
    })
    expect(msgs[1].blocks).toEqual([{ type: 'text', content: 'You now run on drill-sat. Continue the work.' }])
  })

  it('the frame and the row build one message', () => {
    const evt = { prompt: 'Check the upload log.', task_id: 'dyn-c1' }
    const live = wakeMessage(evt, 'wake-1', '2026-10-06T19:10:00+00:00')
    const [stored] = dbMessagesToDisplay([row('event', '', 'schedule_wake', evt)], [])
    expect({ ...live, id: '' }).toEqual({ ...stored, id: '' })
  })
})
