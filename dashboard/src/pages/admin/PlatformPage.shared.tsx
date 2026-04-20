import type { ConcurrencyBucket } from './PlatformPage.types'

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

// Inline saved indicator
export function SavedBadge({ show }: { show: boolean }) {
  if (!show) return null
  return (
    <span className="text-xs text-green-600 dark:text-green-400 font-medium animate-pulse ml-2">
      Saved
    </span>
  )
}

// ---------------------------------------------------------------------------
// Concurrency row (reusable for chat/task/meeting)
// ---------------------------------------------------------------------------

export function ActiveBadge({ bucket }: { bucket?: ConcurrencyBucket }) {
  if (!bucket) return null
  const pct = bucket.limit > 0 ? bucket.active / bucket.limit : 0
  const color =
    pct >= 1 ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400' :
    pct >= 0.7 ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400' :
    'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400'
  return (
    <span className={`text-xs font-mono px-2 py-0.5 rounded-full whitespace-nowrap ${color}`}>
      {bucket.active}/{bucket.limit} active
    </span>
  )
}

export function ConcurrencyRow({
  label, description, bucket, value, onChange, onSave, savedField, fieldKey,
  placeholder, min = 1, forced = false,
}: {
  label: string
  description: string
  bucket?: ConcurrencyBucket
  value: string
  onChange: (v: string) => void
  onSave: () => void
  savedField: string
  fieldKey: string
  placeholder?: string
  min?: number
  forced?: boolean
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="flex-1 min-w-0">
        <label className="block text-sm font-medium text-p-text mb-0.5">{label}</label>
        <p className="text-xs text-p-text-light">{description}</p>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <ActiveBadge bucket={bucket} />
        <input
          type="number"
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          onBlur={onSave}
          min={min}
          disabled={forced}
          title={forced ? 'Managed by the operator' : undefined}
          className="w-24 px-2 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 text-right disabled:opacity-50 disabled:cursor-not-allowed"
        />
        <SavedBadge show={savedField === fieldKey} />
      </div>
    </div>
  )
}
export function QuotaRow({
  label, desc, hint, unit, value, settingKey, forced, onChange, onSave, savedField,
}: {
  label: string
  desc: string
  hint?: string
  unit: string
  value: string
  settingKey: string
  forced: boolean
  onChange: (v: string) => void
  onSave: () => void
  savedField: string
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="flex-1 min-w-0">
        <label className="block text-sm font-medium text-p-text mb-0.5">{label}</label>
        <p className="text-xs text-p-text-light">
          {desc}{hint ? <span className="text-p-text-secondary"> — {hint}</span> : null}
        </p>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <input
          type="number"
          min={0}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onBlur={onSave}
          disabled={forced}
          className="w-24 px-2 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 text-right disabled:opacity-50"
        />
        <span className="text-xs text-p-text-light w-9">{unit}</span>
        <SavedBadge show={savedField === settingKey} />
      </div>
    </div>
  )
}
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
  skipped_in_use: 'skipped (in use)',
  failed: 'failed',
  held: 'held (.hold marker)',
}
