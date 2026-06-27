import { describe, it, expect } from 'vitest'

import { formatCronDescription, formatIntervalDescription } from '@/lib/format'

describe('formatCronDescription', () => {
  it('reads day-of-week as STANDARD cron (0 or 7 = Sunday)', () => {
    // The filmed regression pair: '5' must be Friday, matching what the
    // proxy now fires (it remaps to APScheduler's 0=Monday internally).
    expect(formatCronDescription('0 9 * * 5')).toBe('Weekly on Friday at 09:00')
    expect(formatCronDescription('0 9 * * 0')).toBe('Weekly on Sunday at 09:00')
    expect(formatCronDescription('0 9 * * 7')).toBe('Weekly on Sunday at 09:00')
    expect(formatCronDescription('0 9 * * 1')).toBe('Weekly on Monday at 09:00')
    expect(formatCronDescription('30 17 * * 6')).toBe('Weekly on Saturday at 17:30')
  })

  it('humanizes the */N hours step form', () => {
    expect(formatCronDescription('0 */6 * * *')).toBe('Every 6 hours')
    expect(formatCronDescription('0 */3 * * *')).toBe('Every 3 hours')
    expect(formatCronDescription('15 */2 * * *')).toBe('Every 2 hours at :15')
  })

  it('keeps the existing shapes', () => {
    expect(formatCronDescription('* * * * *')).toBe('Every minute')
    expect(formatCronDescription('*/10 * * * *')).toBe('Every 10 min')
    expect(formatCronDescription('0 9 * * *')).toBe('Daily at 09:00')
    expect(formatCronDescription('0 9 1 * *')).toBe('Monthly on the 1st at 09:00')
    expect(formatCronDescription('0 9 * * 1-5')).toBe('Weekdays at 09:00')
    expect(formatCronDescription('0 9 * * 0,6')).toBe('Weekends at 09:00')
    expect(formatCronDescription('0 9 * * 6,0')).toBe('Weekends at 09:00')
  })

  it('falls back to the raw string for unhandled forms', () => {
    expect(formatCronDescription('0 9 * * 1,3')).toBe('0 9 * * 1,3')
    expect(formatCronDescription('nonsense')).toBe('nonsense')
    expect(formatCronDescription('')).toBe('')
  })
})

describe('formatIntervalDescription', () => {
  it('renders single-unit and compound intervals', () => {
    expect(formatIntervalDescription(60)).toBe('Every minute')
    expect(formatIntervalDescription(3600)).toBe('Every hour')
    expect(formatIntervalDescription(61200)).toBe('Every 17 hours')
    expect(formatIntervalDescription(90061)).toBe('Every 1d 1h 1m 1s')
    expect(formatIntervalDescription(null)).toBe('')
  })
})
