/**
 * History replay of delegate-result rows (the 2026-07-13 incident shapes).
 *
 * A delegate_result event row renders no inline block (eventToBlock returns
 * null — the badge completes via post-processing), so the replay must not
 * mint an empty assistant host for it: that host rendered as a stuck
 * typing-dots stub, and by consuming the turn boundary it made the NEXT
 * assistant row — the delegating agent's synthesis echo — merge into the
 * delegate-response bubble, reading as the delegate's own text.
 */
import { describe, it, expect } from 'vitest'
import { dbMessagesToDisplay } from '@/lib/messageBlocks'

const AGENTS = [
  { name: 'otodock-developer', display_name: 'OtoDock Developer', color: '#2a6df4' },
]

let nextId = 1
function row(role: string, content: string, eventType = '', eventData: object | null = null) {
  return {
    id: nextId++,
    role,
    content,
    event_type: eventType,
    event_data: eventData ? JSON.stringify(eventData) : '',
    created_at: '2026-07-13T11:57:25+00:00',
  }
}

function delegateResultRow(over: object = {}) {
  return row('event', '', 'delegate_result', {
    task_id: 'dyn-1', task_name: 'fix lane', agent: 'otodock-developer',
    output_text: 'WORKER OUTPUT', status: 'completed', ...over,
  })
}

describe('delegate_result history replay', () => {
  it('mints no empty assistant stub and keeps the echo its own message', () => {
    const msgs = dbMessagesToDisplay(
      [
        row('user', 'delegate the fixes'),
        row('assistant', 'Delegating now.'),
        row('event', '', 'delegate_spawn', {
          type: 'delegate_spawn', task_id: 'dyn-1', task_name: 'fix lane',
          agent: 'otodock-developer', prompt_preview: 'fix things',
        }),
        delegateResultRow(),
        row('assistant', 'THE SYNTHESIS ECHO'),
      ],
      AGENTS,
    )
    // No assistant message may be left block-less — an empty one renders as
    // an eternal typing-dots stub in history.
    for (const m of msgs) {
      if (m.role === 'assistant') expect(m.blocks.length).toBeGreaterThan(0)
    }
    // The delegate response is its own badged bubble with ONLY the worker
    // output; the synthesis echo follows as its own identity-less message.
    const delegateMsg = msgs.find((m) => m.badge === 'delegate response')!
    expect(delegateMsg).toBeTruthy()
    expect(delegateMsg.blocks).toEqual([{ type: 'text', content: 'WORKER OUTPUT' }])
    const last = msgs[msgs.length - 1]
    expect(last.blocks).toEqual([{ type: 'text', content: 'THE SYNTHESIS ECHO' }])
    expect(last.badge).toBeUndefined()
    expect(last.agentSlug).toBeUndefined()
    // And the spawn block completed via post-processing.
    const spawnBlock = msgs.flatMap((m) => m.blocks).find((b) => b.type === 'delegate')!
    expect(spawnBlock.status).toBe('completed')
  })

  it('a no-output delegate_result still opens a fresh turn for what follows', () => {
    const msgs = dbMessagesToDisplay(
      [
        row('assistant', 'Delegating now.'),
        delegateResultRow({ output_text: '' }),
        row('assistant', 'REVIEWED THE LANE'),
      ],
      AGENTS,
    )
    expect(msgs).toHaveLength(2)
    expect(msgs[0].blocks).toEqual([{ type: 'text', content: 'Delegating now.' }])
    expect(msgs[1].blocks).toEqual([{ type: 'text', content: 'REVIEWED THE LANE' }])
  })
})
