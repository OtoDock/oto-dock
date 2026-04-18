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
  const ord = (n: number) => `${n}${n === 1 ? 'st' : n === 2 ? 'nd' : n === 3 ? 'rd' : 'th'}`

  if (cron === '* * * * *') return 'Every minute'
  const everyMin = min.match(/^\*\/(\d+)$/)
  if (everyMin && isEvery(hour) && isEvery(dom) && isEvery(dow)) return `Every ${everyMin[1]} min`
  const everyHour = hour.match(/^\*\/(\d+)$/)
  if (isFixed(min) && everyHour && isEvery(dom) && isEvery(dow)) {
    return min === '0'
      ? `Every ${everyHour[1]} hours`
      : `Every ${everyHour[1]} hours at :${min.padStart(2, '0')}`
  }

  if (time && isEvery(dom) && isEvery(dow)) return `Daily at ${time}`
  if (time && isEvery(dom) && isFixed(dow)) return `Weekly on ${+dow <= 7 ? DOW[+dow % 7] : dow} at ${time}`
  if (time && isFixed(dom) && isEvery(dow)) return `Monthly on the ${ord(+dom)} at ${time}`
  if (time && dom.includes(',') && isEvery(dow)) {
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
