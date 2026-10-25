import type { ConcurrencyBucket } from './PlatformPage.types'
import { Input } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE } from '../../components/ui/classes'
import { SaveState, SettingRow } from '../../components/ui/SettingsControls'
import type { SaveStatus } from '../../hooks/useSaveState'

// ---------------------------------------------------------------------------
// Concurrency row (reusable for chat/task/meeting)
// ---------------------------------------------------------------------------

export function ActiveBadge({ bucket }: { bucket?: ConcurrencyBucket }) {
  if (!bucket) return null
  const pct = bucket.limit > 0 ? bucket.active / bucket.limit : 0
  const tone = pct >= 1 ? 'danger' : pct >= 0.7 ? 'warning' : 'neutral'
  return (
    <span className={`${BADGE_BASE} ${BADGE_TONE[tone]} font-mono`}>
      {bucket.active}/{bucket.limit} active
    </span>
  )
}

export function ConcurrencyRow({
  label, info, bucket, value, onChange, onSave, save, fieldKey,
  placeholder, min = 1, forced = false,
}: {
  label: string
  info: string
  bucket?: ConcurrencyBucket
  value: string
  onChange: (v: string) => void
  onSave: () => void
  save: SaveStatus
  fieldKey: string
  placeholder?: string
  min?: number
  forced?: boolean
}) {
  const inputId = `concurrency-${fieldKey}`
  return (
    <SettingRow label={label} info={info} htmlFor={inputId} save={save}>
      <ActiveBadge bucket={bucket} />
      <Input
        id={inputId}
        type="number"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onSave}
        min={min}
        disabled={forced}
        title={forced ? 'Managed by the operator' : undefined}
        className="max-w-24 text-right"
      />
    </SettingRow>
  )
}
export function QuotaRow({
  label, desc, hint, unit, value, forced, onChange, onSave, save,
}: {
  label: string
  desc: string
  hint?: string
  unit: string
  value: string
  forced: boolean
  onChange: (v: string) => void
  onSave: () => void
  save: SaveStatus
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-primary mb-0.5"><label>{label}</label><SaveState state={save} /></p>
        <p className="text-xs text-tertiary">
          {desc}{hint ? <span className="text-secondary"> — {hint}</span> : null}
        </p>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <Input
          type="number"
          min={0}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onBlur={onSave}
          disabled={forced}
          className="max-w-24 text-right"
        />
        <span className="text-xs text-tertiary w-9">{unit}</span>
      </div>
    </div>
  )
}
