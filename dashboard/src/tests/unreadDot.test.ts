import { describe, it, expect } from 'vitest'
import { unreadRowClass } from '@/lib/chat/unreadRow'


const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()

describe('unreadRowClass — unread row tint fades with response age', () => {
  it('fresh response (<6h) gets the full action-subtle tint', () => {
    expect(unreadRowClass(hoursAgo(0.1))).toBe('bg-action-subtle')
    expect(unreadRowClass(hoursAgo(5.5))).toBe('bg-action-subtle')
  })

  it('same-day response (6-24h) fades one step', () => {
    expect(unreadRowClass(hoursAgo(7))).toBe('bg-action-subtle/60')
    expect(unreadRowClass(hoursAgo(23))).toBe('bg-action-subtle/60')
  })

  it('older than a day fades to the faintest step', () => {
    expect(unreadRowClass(hoursAgo(25))).toBe('bg-action-subtle/35')
    expect(unreadRowClass(hoursAgo(24 * 14))).toBe('bg-action-subtle/35')
  })

  it('missing or unparsable timestamp counts as fresh', () => {
    expect(unreadRowClass(undefined)).toBe('bg-action-subtle')
    expect(unreadRowClass(null)).toBe('bg-action-subtle')
    expect(unreadRowClass('not-a-date')).toBe('bg-action-subtle')
  })
})
