import { describe, it, expect } from 'vitest'
import { unreadRowClass } from '../components/chat/ChatHistory'

const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()

describe('unreadRowClass — unread row tint fades with response age', () => {
  it('fresh response (<6h) gets the full brand-surface tint', () => {
    expect(unreadRowClass(hoursAgo(0.1))).toBe('bg-brand-surface')
    expect(unreadRowClass(hoursAgo(5.5))).toBe('bg-brand-surface')
  })

  it('same-day response (6-24h) fades one step', () => {
    expect(unreadRowClass(hoursAgo(7))).toBe('bg-brand-surface/60')
    expect(unreadRowClass(hoursAgo(23))).toBe('bg-brand-surface/60')
  })

  it('older than a day fades to the faintest step', () => {
    expect(unreadRowClass(hoursAgo(25))).toBe('bg-brand-surface/35')
    expect(unreadRowClass(hoursAgo(24 * 14))).toBe('bg-brand-surface/35')
  })

  it('missing or unparsable timestamp counts as fresh', () => {
    expect(unreadRowClass(undefined)).toBe('bg-brand-surface')
    expect(unreadRowClass(null)).toBe('bg-brand-surface')
    expect(unreadRowClass('not-a-date')).toBe('bg-brand-surface')
  })
})
