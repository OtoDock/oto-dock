// Formatting and option lists shared by the Setup tabs (the components
// they share live in PlatformPage.shared.tsx).

export function formatBytes(n: number | undefined): string {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`
}

// Common timezone options
export const TIMEZONE_OPTIONS = [
  'UTC',
  'US/Eastern', 'US/Central', 'US/Mountain', 'US/Pacific',
  'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Athens', 'Europe/Moscow',
  'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dubai',
  'Australia/Sydney', 'Pacific/Auckland',
  'America/Sao_Paulo', 'Africa/Cairo',
]

export function relativeTime(iso: string): string {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60); if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60); if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24); return `${d}d ago`
}

export const MCP_STATUS_LABEL: Record<string, string> = {
  updated: 'updated',
  no_change: 'no change',
  skipped_in_use: 'skipped while in use',
  failed: 'failed',
  held: 'held by a .hold file',
  needs_approval: 'new source, needs approval',
}
