import { TASK_KIND } from './kinds/task'

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const mins = Math.floor(ms / 60_000)
  const secs = Math.floor((ms % 60_000) / 1000)
  return `${mins}m ${secs}s`
}

export function formatRelativeTime(iso: string): string {
  const now = Date.now()
  const then = new Date(iso).getTime()
  const diff = Math.floor((now - then) / 1000)
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return new Date(iso).toLocaleDateString()
}

/** An expiry in words: "expires in 12 days", "expires within a day", "expired";
 *  '' for no expiry. Days round up, so a link made an hour ago for 30 days
 *  still says 30. */
export function formatExpiry(iso: string | null | undefined): string {
  if (!iso) return ''
  const days = Math.ceil((new Date(iso).getTime() - Date.now()) / 86_400_000)
  if (Number.isNaN(days)) return ''
  if (days <= 0) return 'expired'
  return days === 1 ? 'expires within a day' : `expires in ${days} days`
}

export function formatNextRun(iso: string | null): string {
  if (!iso) return '—'
  const now = Date.now()
  const then = new Date(iso).getTime()
  const diff = Math.floor((then - now) / 1000) // positive = future
  if (diff < 0) return 'overdue'
  if (diff < 60) return `in ${diff}s`
  if (diff < 3600) return `in ${Math.floor(diff / 60)}m`
  if (diff < 86400) {
    const h = Math.floor(diff / 3600)
    const m = Math.floor((diff % 3600) / 60)
    return m > 0 ? `in ${h}h ${m}m` : `in ${h}h`
  }
  return new Date(iso).toLocaleString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

// Day-of-week follows STANDARD cron (0 or 7 = Sunday) — the platform's
// user-facing convention everywhere; the proxy remaps to APScheduler's
// 0=Monday numbering internally at trigger construction.
export function formatCronDescription(cron: string): string {
  if (!cron) return ''
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return cron
  const [min, hour, dom, , dow] = parts

  const isEvery = (p: string) => p === '*'
  const isFixed = (p: string) => /^\d+$/.test(p)
  const time = (isFixed(hour) && isFixed(min))
    ? `${hour.padStart(2, '0')}:${min.padStart(2, '0')}`
    : null
  const DOW = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
  const ord = (n: number) => {
    const suffix = n % 100 >= 10 && n % 100 <= 20 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[n % 10] ?? 'th'
    return `${n}${suffix}`
  }

  if (cron === '* * * * *') return 'Every minute'
  const everyMin = min.match(/^\*\/(\d+)$/)
  if (everyMin && isEvery(hour) && isEvery(dom) && isEvery(dow)) return `Every ${everyMin[1]} minutes`
  const everyHour = hour.match(/^\*\/(\d+)$/)
  if (isFixed(min) && everyHour && isEvery(dom) && isEvery(dow)) {
    return min === '0'
      ? `Every ${everyHour[1]} hours`
      : `Every ${everyHour[1]} hours at :${min.padStart(2, '0')}`
  }

  if (time && isEvery(dom) && isEvery(dow)) return `Daily at ${time}`
  if (time && isEvery(dom) && isFixed(dow)) return `Weekly on ${+dow <= 7 ? DOW[+dow % 7] : dow} at ${time}`
  if (time && isFixed(dom) && isEvery(dow)) return `Monthly on the ${ord(+dom)} at ${time}`
  if (time && dom.includes(',') && isEvery(dow) && dom.split(',').every(isFixed)) {
    const days = dom.split(',').map((d) => ord(+d)).join(' & ')
    return `On the ${days} of each month at ${time}`
  }
  if (time && isEvery(dom) && dow === '1-5') return `Weekdays at ${time}`
  if (time && isEvery(dom) && (dow === '0,6' || dow === '6,0')) return `Weekends at ${time}`

  return cron
}

// Renders an "every N seconds" recurring schedule as a human-readable string.
// Pairs with formatCronDescription — both return '' on null/empty so callers
// can use ||-chains for the timing column.
//
// Examples:
//   60     → "Every minute"
//   1800   → "Every 30 minutes"
//   3600   → "Every hour"
//   61200  → "Every 17 hours"
//   86400  → "Every day"
//   172800 → "Every 2 days"
//   90061  → "Every 1d 1h 1m 1s"
export function formatIntervalDescription(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return ''
  const SECOND = 1
  const MINUTE = 60
  const HOUR = 3600
  const DAY = 86400

  // Single-unit pretty cases first.
  if (seconds === MINUTE) return 'Every minute'
  if (seconds === HOUR) return 'Every hour'
  if (seconds === DAY) return 'Every day'

  // Exact multiples of a single unit.
  if (seconds % DAY === 0) {
    const d = seconds / DAY
    return `Every ${d} days`
  }
  if (seconds % HOUR === 0) {
    const h = seconds / HOUR
    return `Every ${h} hours`
  }
  if (seconds % MINUTE === 0) {
    const m = seconds / MINUTE
    return `Every ${m} minutes`
  }

  // Compound fallback (e.g. 90061 → "1d 1h 1m 1s").
  let remaining = seconds
  const parts: string[] = []
  const d = Math.floor(remaining / DAY); remaining -= d * DAY
  const h = Math.floor(remaining / HOUR); remaining -= h * HOUR
  const m = Math.floor(remaining / MINUTE); remaining -= m * MINUTE
  const s = remaining / SECOND
  if (d) parts.push(`${d}d`)
  if (h) parts.push(`${h}h`)
  if (m) parts.push(`${m}m`)
  if (s) parts.push(`${s}s`)
  return `Every ${parts.join(' ')}`
}

// A task's schedule in words — the same vocabulary as the proxy's
// services/scheduler/schedule_text.py, which writes `schedule_text` for the
// apps `tasks` feed and the task REST view; tests/fixtures/scheduleText.json
// pins both sides case by case, so a change here is a change there.

/** The fields the words need: a task row, or an app feed row. */
export interface ScheduleRef {
  schedule?: string | null
  interval_seconds?: number | null
  run_at?: string | null
  delay_seconds?: number | null
  task_type?: string
  user_tz?: string | null
  effective_tz?: string
}

const DOW_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MON_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "5 minutes", "1 hour", "2 days", "1d 1h 1m 1s" (the interval words minus "Every"). */
function durationWords(seconds: number): string {
  if (seconds === 60) return '1 minute'
  if (seconds === 3600) return '1 hour'
  if (seconds === 86400) return '1 day'
  const words = formatIntervalDescription(seconds)
  return words.startsWith('Every ') ? words.slice(6) : words
}

/** Whether a cron has a plain hour and minute ("0 8 * * *"), i.e. a wall clock. */
function cronHasClock(cron: string): boolean {
  const parts = cron.trim().split(/\s+/)
  return parts.length === 5 && /^\d+$/.test(parts[0]) && /^\d+$/.test(parts[1])
}

/** "Once on Sun 20 Sep, 05:00": a naive ISO is a wall clock in the task's zone
 *  and prints literally (never through Date, which would shift it into the
 *  browser's zone); an aware one is converted into that zone. No year, no
 *  locale: the proxy prints the same. */
function runAtWords(runAt: string, zone: string): string {
  const naive = runAt.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?$/)
  let y: number, mo: number, d: number, hh: string, mm: string
  if (naive) {
    ;[y, mo, d] = [+naive[1], +naive[2], +naive[3]]
    ;[hh, mm] = [naive[4], naive[5]]
  } else {
    const date = new Date(runAt)
    if (Number.isNaN(date.getTime())) return `Once on ${runAt}`
    let parts: Intl.DateTimeFormatPart[]
    try {
      parts = new Intl.DateTimeFormat('en-US', {
        timeZone: zone || undefined, year: 'numeric', month: 'numeric', day: 'numeric',
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      }).formatToParts(date)
    } catch {
      parts = new Intl.DateTimeFormat('en-US', {
        year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      }).formatToParts(date)
    }
    const get = (type: string) => parts.find((p) => p.type === type)?.value ?? ''
    ;[y, mo, d] = [+get('year'), +get('month'), +get('day')]
    ;[hh, mm] = [get('hour').padStart(2, '0'), get('minute').padStart(2, '0')]
  }
  const dow = DOW_SHORT[new Date(Date.UTC(y, mo - 1, d)).getUTCDay()]
  return `Once on ${dow} ${d} ${MON_SHORT[mo - 1]}, ${hh}:${mm}`
}

export function scheduleZone(task: ScheduleRef): string {
  return task.effective_tz || task.user_tz || ''
}

/** Whether the words carry a wall-clock time, i.e. depend on the zone. */
export function scheduleHasClock(task: ScheduleRef): boolean {
  if (task.task_type === TASK_KIND.TRIGGER) return false
  if (task.run_at) return true
  const cron = task.schedule || ''
  return !!cron && cronHasClock(cron) && formatCronDescription(cron) !== cron
}

/** The schedule in words, without the zone. */
export function scheduleWords(task: ScheduleRef): string {
  if (task.task_type === TASK_KIND.TRIGGER) return 'On trigger'
  if (task.interval_seconds) return formatIntervalDescription(task.interval_seconds)
  if (task.schedule) return formatCronDescription(task.schedule)
  if (task.run_at) return runAtWords(task.run_at, scheduleZone(task))
  if (task.delay_seconds != null) return `Once, ${durationWords(task.delay_seconds)} after creation`
  return '—'
}

// The names of the zero-offset zone: a browser says "UTC", a platform
// setting "Etc/UTC" — the same clock, so no suffix between them.
const UTC_NAMES = new Set([
  'UTC', 'Etc/UTC', 'Etc/GMT', 'GMT', 'Etc/GMT0', 'GMT0', 'Etc/GMT+0', 'Etc/GMT-0',
  'Etc/Universal', 'Universal', 'Etc/Zulu', 'Zulu', 'Etc/UCT', 'UCT', 'Etc/Greenwich', 'Greenwich',
])

/** Whether two IANA names are the same clock (only the UTC aliases fold; two
 *  real zones sharing an offset are still named apart, the offset can drift). */
export function sameZone(a: string, b: string): boolean {
  return a === b || (UTC_NAMES.has(a) && UTC_NAMES.has(b))
}

/** The words, with the zone named when they carry a clock time and the task's
 *  zone is not the browser's — "Daily at 08:00 (Europe/Athens)" beside a
 *  "Next" the browser computes in its own zone. */
export function describeSchedule(task: ScheduleRef, browserTz: string): string {
  const text = scheduleWords(task)
  const zone = scheduleZone(task)
  return zone && !sameZone(zone, browserTz) && scheduleHasClock(task) ? `${text} (${zone})` : text
}

/** The browser's own IANA zone ('' when the runtime cannot say). */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || ''
  } catch {
    return ''
  }
}
