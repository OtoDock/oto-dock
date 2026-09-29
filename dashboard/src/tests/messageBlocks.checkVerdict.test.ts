/**
 * A check's verdict (CHECKS.md "Rendering") is a persisted `check_verdict`
 * event row; history rebuilds it into the compact `checkverdict` block with
 * the fields the card shows, and a live frame goes through the same
 * eventToBlock.
 */
import { describe, it, expect } from 'vitest'
import { dbMessagesToDisplay, eventToBlock } from '@/lib/messageBlocks'

const AGENTS = [{ name: 'alpha', display_name: 'Alpha', color: '#2a6df4' }]

let nextId = 1
function row(role: string, content: string, eventType = '', eventData: object | null = null) {
  return {
    id: nextId++, role, content, event_type: eventType,
    event_data: eventData ? JSON.stringify(eventData) : '', created_at: '2026-09-17T10:00:00+00:00',
  }
}

// The persisted row is the event itself, `type` included.
const VERDICT = {
  type: 'check_verdict', check: 'coding', ref: 'agent:coding', section: 'judge', status: 'fail', pass: false, score: 0.4,
  summary: 'The commit message names no file.',
  findings: [{ location: 'src/a.py:12', severity: 'error', text: 'unused import' }],
  findings_total: 4, round: 1, rounds: 3, ran_on: 'local', cost_usd: 0.1234, duration_ms: 9000,
  verdict_id: 'v-one',
}

describe('the check verdict block', () => {
  it('rebuilds from the persisted event row with the card fields', () => {
    const msgs = dbMessagesToDisplay(
      [row('user', 'fix it'), row('assistant', 'done'), row('event', '', 'check_verdict', VERDICT)], AGENTS,
    )
    const b = msgs.flatMap((m) => m.blocks).find((x) => x.type === 'checkverdict')
    expect(b).toBeTruthy()
    if (!b || b.type !== 'checkverdict') return
    expect(b.check).toBe('coding')
    expect(b.status).toBe('fail')
    expect(b.score).toBe(0.4)
    expect(b.findings).toHaveLength(1)
    expect(b.findingsTotal).toBe(4)
    expect(b.round).toBe(1)
    expect(b.rounds).toBe(3)
    expect(b.costUsd).toBeCloseTo(0.1234)
    expect(b.verdictId).toBe('v-one')
  })

  it('takes a live frame through the same path and tolerates a bare event', () => {
    const live = eventToBlock({ ...VERDICT, chat_id: 'c1' })
    expect(live && live.type === 'checkverdict' && live.summary).toBe('The commit message names no file.')
    const bare = eventToBlock({ type: 'check_verdict', check: 'x', status: 'skipped' })
    expect(bare && bare.type === 'checkverdict' && bare.findings).toEqual([])
    expect(bare && bare.type === 'checkverdict' && bare.findingsTotal).toBe(0)
    expect(bare && bare.type === 'checkverdict' && bare.round).toBe(1)
  })
})
