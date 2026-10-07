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

  it('a deleted delegate agent keeps its slug (no display name resolved)', () => {
    const msgs = dbMessagesToDisplay(
      [row('assistant', 'Delegating now.'), delegateResultRow({ agent: 'ghost-agent' })],
      AGENTS,
    )
    const delegateMsg = msgs.find((m) => m.badge === 'delegate response')!
    expect(delegateMsg.agentSlug).toBe('ghost-agent')
    expect(delegateMsg.agentDisplayName).toBeUndefined()
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

describe('delegate_result rows that carry files', () => {
  const FILES = [{ path: 'users/alice/workspace/inbox/otodock-developer/report.md', bytes: 12600 }]
  const SKIPPED = [{ path: 'notes/x.md', reason: 'symlink' }]

  it('appends the files list to the worker response bubble, after its text', () => {
    const msgs = dbMessagesToDisplay(
      [row('assistant', 'Delegating now.'), delegateResultRow({ files: FILES, files_skipped: SKIPPED })],
      AGENTS,
    )
    const bubble = msgs.find((m) => m.badge === 'delegate response')!
    expect(bubble.blocks).toEqual([
      { type: 'text', content: 'WORKER OUTPUT' },
      { type: 'delegate_files', files: FILES, skipped: SKIPPED },
    ])
  })

  it('a files-only result mints the bubble with the list alone and keeps the echo apart', () => {
    const msgs = dbMessagesToDisplay(
      [
        row('assistant', 'Delegating now.'),
        row('event', '', 'delegate_spawn', {
          type: 'delegate_spawn', task_id: 'dyn-1', task_name: 'fix lane',
          agent: 'otodock-developer', prompt_preview: 'fix things',
        }),
        delegateResultRow({ output_text: '', files: FILES, files_skipped: [] }),
        row('assistant', 'THE SYNTHESIS ECHO'),
      ],
      AGENTS,
    )
    const bubble = msgs.find((m) => m.badge === 'delegate response')!
    expect(bubble.blocks).toEqual([{ type: 'delegate_files', files: FILES, skipped: [] }])
    expect(bubble.agentSlug).toBe('otodock-developer')
    const last = msgs[msgs.length - 1]
    expect(last.blocks).toEqual([{ type: 'text', content: 'THE SYNTHESIS ECHO' }])
    expect(last.badge).toBeUndefined()
    for (const m of msgs) if (m.role === 'assistant') expect(m.blocks.length).toBeGreaterThan(0)
    expect(msgs.flatMap((m) => m.blocks).find((b) => b.type === 'delegate')!.status).toBe('completed')
  })

  it('empty lists and no text mint nothing', () => {
    const msgs = dbMessagesToDisplay(
      [row('assistant', 'Delegating now.'), delegateResultRow({ output_text: '', files: [], files_skipped: [] })],
      AGENTS,
    )
    expect(msgs).toHaveLength(1)
    expect(msgs.find((m) => m.badge === 'delegate response')).toBeUndefined()
  })

  it('a failed result keeps its badge with the files', () => {
    const msgs = dbMessagesToDisplay(
      [row('assistant', 'x'), delegateResultRow({ status: 'failed', output_text: '⚠ failed', files: FILES })],
      AGENTS,
    )
    const bubble = msgs.find((m) => m.badge === 'delegate failed')!
    expect(bubble.blocks[1]).toEqual({ type: 'delegate_files', files: FILES, skipped: [] })
  })
})
