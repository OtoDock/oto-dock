import type { LimitPayload, PoolCapField, PoolCapStatus } from '@/api/usage'

// The two engines that carry subscription accounts, and what the accounts are.
export const ENGINE_NAMES: Record<string, string> = {
  'claude-code-cli': 'Claude Code',
  'codex-cli': 'Codex',
}
export const ACCOUNT_NAMES: Record<string, string> = {
  'claude-code-cli': 'Claude',
  'codex-cli': 'ChatGPT',
}

export const CAP_FIELDS: PoolCapField[] = ['week_pct', 'day_pct', 'week_usd', 'day_usd']

// `short` names the reading on the Usage pages: the percentages are of the
// accounts' weekly window (now, and the share used in the last 24 h), the
// dollars are the equivalent API cost over a rolling 7 days / 24 h — never
// a calendar week or day, which is why "today" is not said.
const FIELD_TEXT: Record<PoolCapField, { period: string; short: string; unit: 'pct' | 'usd' }> = {
  week_pct: { period: 'the week', short: 'Weekly window', unit: 'pct' },
  day_pct: { period: 'today', short: 'Window, last 24 h', unit: 'pct' },
  week_usd: { period: 'the week', short: 'API cost, 7 days', unit: 'usd' },
  day_usd: { period: 'today', short: 'API cost, 24 h', unit: 'usd' },
}

export function engineName(layer: string): string {
  return ENGINE_NAMES[layer] || layer
}

function num(x: number): string {
  return String(Math.round(x * 100) / 100)
}

export function formatReading(key: PoolCapField, value: number | null): string {
  if (value == null) return '—'
  return FIELD_TEXT[key].unit === 'pct' ? `${Math.round(value)}%` : `$${value.toFixed(2)}`
}

export function formatCap(key: PoolCapField, value: number | null): string {
  if (value == null) return ''
  return FIELD_TEXT[key].unit === 'pct' ? `${num(value)}%` : `$${num(value)}`
}

export function fieldShort(key: PoolCapField): string {
  return FIELD_TEXT[key].short
}

// "the week is at 52% of the 50% cap" — the same wording the proxy uses.
export function describeReading(s: PoolCapStatus, key: PoolCapField): string {
  const cap = s.caps[key]
  const reading = s.readings[key]
  if (cap == null || reading == null) return ''
  return `${FIELD_TEXT[key].period} is at ${formatReading(key, reading)} of the ${formatCap(key, cap)} cap`
}

export function hitText(s: PoolCapStatus): string {
  return s.hits.length ? describeReading(s, s.hits[0]) : ''
}

// The first reading at or past 80 % of its cap.
export function warningField(s: PoolCapStatus): PoolCapField | null {
  for (const key of CAP_FIELDS) {
    const cap = s.caps[key]
    const reading = s.readings[key]
    if (cap != null && cap > 0 && reading != null && reading >= cap * 0.8) return key
  }
  return null
}

// Colour of one reading against its cap: hit, near, or plain.
export function readingTone(s: PoolCapStatus, key: PoolCapField): 'error' | 'warn' | 'plain' {
  const cap = s.caps[key]
  const reading = s.readings[key]
  if (cap == null || cap <= 0 || reading == null) return 'plain'
  if (reading >= cap) return 'error'
  if (reading >= cap * 0.8) return 'warn'
  return 'plain'
}

// The composer's red banner: which budget blocked the turn.
export function describeLimitReached(info: LimitPayload | null): { title: string; body: string } {
  if (info?.pool) {
    const s = info.pool
    const hit = hitText(s)
    return {
      title: 'Subscription cap reached.',
      body: `${engineName(s.layer)}${hit ? `: ${hit}` : ''}. It clears as the accounts' windows reset; `
        + (s.scope === 'user' ? 'change the cap in User Settings → Usage.' : 'an admin can change the cap in Setup → Usage.'),
    }
  }
  const self = info?.self
  const own = self?.monthly?.percent != null && self.monthly.percent >= 100
    ? self.monthly
    : self?.weekly?.percent != null && self.weekly.percent >= 100 ? self.weekly : null
  if (own) {
    return {
      title: 'Your API-key budget is reached.',
      body: `$${own.used.toFixed(2)} of $${(own.limit ?? 0).toFixed(2)} spent on your own keys. Raise or clear it in User Settings → Usage.`,
    }
  }
  return { title: 'Usage limit reached.', body: 'Contact your administrator to increase your limit.' }
}

// The composer's amber toast text.
export function describeLimitWarning(info: LimitPayload | null): string {
  if (info?.pool) {
    const s = info.pool
    if (s.hits.length) return `Subscription cap reached (${hitText(s)}); continuing on an API key.`
    const key = warningField(s)
    if (key) return `Subscription cap: ${describeReading(s, key)} on ${engineName(s.layer)}.`
  }
  const self = info?.self
  for (const period of ['monthly', 'weekly'] as const) {
    const p = self?.[period]
    if (p && p.percent >= 80) {
      return `You've used ${p.percent}% of your ${period} API-key budget ($${p.used.toFixed(2)} / $${(p.limit ?? 0).toFixed(2)}).`
    }
  }
  if (info?.monthly && info.monthly.percent >= 80) {
    return `You've used ${info.monthly.percent}% of your monthly limit ($${info.monthly.used.toFixed(2)} / $${info.monthly.limit?.toFixed(2)}).`
  }
  if (info?.weekly && info.weekly.percent >= 80) {
    return `You've used ${info.weekly.percent}% of your weekly limit ($${info.weekly.used.toFixed(2)} / $${info.weekly.limit?.toFixed(2)}).`
  }
  return 'You are approaching your usage limit.'
}
