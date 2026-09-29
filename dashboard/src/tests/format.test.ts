import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  formatCronDescription, formatExpiry, formatIntervalDescription, describeSchedule, scheduleWords, type ScheduleRef,
} from '@/lib/format'

const here = path.dirname(fileURLToPath(import.meta.url))

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
    expect(formatCronDescription('*/10 * * * *')).toBe('Every 10 minutes')
    expect(formatCronDescription('0 9 * * *')).toBe('Daily at 09:00')
    expect(formatCronDescription('0 9 1 * *')).toBe('Monthly on the 1st at 09:00')
    expect(formatCronDescription('0 9 * * 1-5')).toBe('Weekdays at 09:00')
    expect(formatCronDescription('0 9 * * 0,6')).toBe('Weekends at 09:00')
    expect(formatCronDescription('0 9 * * 6,0')).toBe('Weekends at 09:00')
  })

  it('spells every day of the month as the proxy does', () => {
    expect(formatCronDescription('0 9 21 * *')).toBe('Monthly on the 21st at 09:00')
    expect(formatCronDescription('0 9 22 * *')).toBe('Monthly on the 22nd at 09:00')
    expect(formatCronDescription('0 9 23 * *')).toBe('Monthly on the 23rd at 09:00')
    expect(formatCronDescription('0 9 31 * *')).toBe('Monthly on the 31st at 09:00')
    expect(formatCronDescription('0 9 11 * *')).toBe('Monthly on the 11th at 09:00')
    expect(formatCronDescription('0 9 12,13 * *')).toBe('On the 12th & 13th of each month at 09:00')
  })

  it('falls back to the raw string for unhandled forms', () => {
    expect(formatCronDescription('0 9 * * 1,3')).toBe('0 9 * * 1,3')
    expect(formatCronDescription('0 8 1,15-20 * *')).toBe('0 8 1,15-20 * *')
    expect(formatCronDescription('nonsense')).toBe('nonsense')
    expect(formatCronDescription('')).toBe('')
  })
})

describe('formatExpiry', () => {
  it('never calls a same-day expiry tomorrow', () => {
    const inHours = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()
    expect(formatExpiry(inHours(2))).toBe('expires within a day')
    expect(formatExpiry(inHours(24 * 12 - 1))).toBe('expires in 12 days')
    expect(formatExpiry(inHours(-1))).toBe('expired')
    expect(formatExpiry(null)).toBe('')
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

// The schedule in words, pinned case by case against the proxy's
// services/scheduler/schedule_text.py through the one fixture both sides
// read (proxy/tests/tasks/test_schedule_text.py reads the same file):
// `text` is the zone-free wording, `with_zone` names the task's zone when the
// words carry a clock time — what the tab shows when the browser sits in
// another zone, and what the apps feed sends as schedule_text.
describe('describeSchedule', () => {
  const cases = JSON.parse(readFileSync(path.resolve(here, 'fixtures/scheduleText.json'), 'utf8')) as
    { name: string; task: ScheduleRef; text: string; with_zone: string }[]
  it.each(cases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    expect(scheduleWords(c.task)).toBe(c.text)
    expect(describeSchedule(c.task, 'Pacific/Kiritimati')).toBe(c.with_zone)
    // The browser in the task's own zone: no suffix.
    expect(describeSchedule(c.task, c.task.effective_tz ?? '')).toBe(c.text)
  })

  it('falls back to the row zone and to nothing', () => {
    expect(describeSchedule({ schedule: '0 8 * * *', user_tz: 'Asia/Tokyo' }, 'Europe/Athens')).toBe('Daily at 08:00 (Asia/Tokyo)')
    expect(describeSchedule({ schedule: '0 8 * * *' }, 'Europe/Athens')).toBe('Daily at 08:00')
    expect(describeSchedule({ run_at: 'not a date', effective_tz: 'Europe/Athens' }, 'Etc/UTC')).toBe('Once on not a date (Europe/Athens)')
  })

  it('reads the UTC aliases as one clock (a browser says UTC, a platform Etc/UTC)', () => {
    expect(describeSchedule({ schedule: '0 8 * * *', effective_tz: 'Etc/UTC' }, 'UTC')).toBe('Daily at 08:00')
    expect(describeSchedule({ schedule: '0 8 * * *', effective_tz: 'GMT' }, 'Etc/UTC')).toBe('Daily at 08:00')
    expect(describeSchedule({ schedule: '0 8 * * *', effective_tz: 'Europe/London' }, 'UTC')).toBe('Daily at 08:00 (Europe/London)')
  })
})
