/**
 * `cost_billed` on the persisted metadata row → `costBilled` on the block,
 * and the chat-level gauge rule (newest turn wins). Only an explicit `false`
 * hides a cost: rows written before the flag existed carry none and must keep
 * showing it — the back-compat contract the dashboard owns.
 */
import { describe, it, expect } from 'vitest'
import {
  costBilledOf, eventToBlock, latestCostBilled, liveBlockToMessageBlock, dbMessagesToDisplay,
} from '@/lib/messageBlocks'

let nextId = 1
function row(role: string, content: string, eventType = '', eventData: object | null = null) {
  return {
    id: nextId++,
    role,
    content,
    event_type: eventType,
    event_data: eventData ? JSON.stringify(eventData) : '',
    created_at: '2026-09-08T00:00:00+00:00',
  }
}

describe('costBilledOf', () => {
  it('only an explicit false hides; missing stays undefined', () => {
    expect(costBilledOf({ cost_billed: false })).toBe(false)
    expect(costBilledOf({ cost_billed: true })).toBe(true)
    expect(costBilledOf({})).toBeUndefined()
    expect(costBilledOf({ cost_billed: 'no' as unknown as boolean })).toBeUndefined()
  })
})

describe('metadata block mapping', () => {
  it('carries the flag from a history row and from a live block', () => {
    expect(eventToBlock({ type: 'metadata', cost_usd: 0.05, duration_ms: 900, cost_billed: false }))
      .toEqual({ type: 'metadata', costUsd: 0.05, durationMs: 900, costBilled: false })
    expect(liveBlockToMessageBlock({ type: 'metadata', cost_usd: 0.05, duration_ms: 900, cost_billed: true }))
      .toEqual({ type: 'metadata', costUsd: 0.05, durationMs: 900, costBilled: true })
  })

  it('leaves the flag undefined on a pre-flag row (shown)', () => {
    expect(eventToBlock({ type: 'metadata', cost_usd: 0.05, duration_ms: 900 }))
      .toEqual({ type: 'metadata', costUsd: 0.05, durationMs: 900, costBilled: undefined })
    const msgs = dbMessagesToDisplay(
      [row('user', 'hi'), row('assistant', 'hello'), row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.05 })],
      [],
    )
    const meta = msgs[msgs.length - 1].blocks.find((b) => b.type === 'metadata')
    expect(meta).toEqual({ type: 'metadata', costUsd: 0.05, durationMs: 0, costBilled: undefined })
  })
})

describe('latestCostBilled (gauge rule)', () => {
  it('follows the newest metadata row', () => {
    const rows = [
      row('user', 'a'),
      row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.1, cost_billed: true }),
      row('user', 'b'),
      row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.1, cost_billed: false }),
      row('event', '', 'tool', { type: 'tool', name: 'Bash' }),
    ]
    expect(latestCostBilled(rows)).toBe(false)
    // An API-key turn after a subscription turn shows the gauge again.
    rows.push(row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.1, cost_billed: true }))
    expect(latestCostBilled(rows)).toBe(true)
  })

  it('shows when no loaded turn carries the flag or the chat is empty', () => {
    expect(latestCostBilled([])).toBe(true)
    expect(latestCostBilled([
      row('user', 'a'),
      row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.1 }),
    ])).toBe(true)
    expect(latestCostBilled([
      { id: 1, role: 'event', event_type: 'metadata', event_data: '{not json', created_at: '' },
    ])).toBe(true)
  })
})
