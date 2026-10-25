import { useState } from 'react'
import {
  PoolCapResponse, PoolCapStatus, PoolCapUpdate, PoolCapFields, PoolCapField,
  useMyPoolCap, useSetMyPoolCap, useAdminPoolCap, useSetAdminPoolCap,
} from '@/api/usage'
import { useExecutionLayers } from '@/api/agents'
import { engineLabels } from '@/lib/engines'
import {
  CAP_FIELDS, engineName, fieldShort, formatCap, formatReading, readingTone,
} from './poolCap'
import Button from '../ui/Button'
import { Input, Select } from '../ui/Field'
import { CARD } from '../ui/classes'
import InfoTip from '../ui/InfoTip'

// One subscription pool's readings per engine and its cap form. The
// platform pool (Setup → Usage) and each user's own accounts (User
// Settings → Usage) share this; the data hooks differ.

const TONE_CLASS = {
  error: 'text-status-danger-fg font-medium',
  warn: 'text-status-warning-fg font-medium',
  plain: 'text-secondary',
}

// One engine's pool: the name and account count, then one labelled cell per
// reading (two per row on a phone, four on a desktop) — a run-on line of
// numbers wrapped unreadably on a narrow screen.
function EngineLine({ status }: { status: PoolCapStatus }) {
  // The engine's name comes from the catalog's descriptor (its id when the
  // catalog has not loaded or does not know the engine).
  const { data: layers } = useExecutionLayers()
  return (
    <div data-testid="pool-engine" className="space-y-1.5">
      <div className="flex items-baseline gap-2 text-sm">
        <span className="font-medium text-primary">{engineName(status.layer, engineLabels(layers))}</span>
        <span className="text-xs text-tertiary">
          {status.accounts} {status.accounts === 1 ? 'account' : 'accounts'}
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1.5">
        {CAP_FIELDS.map(key => (
          <div key={key} data-testid={`pool-reading-${key}`}
            title={status.caps[key] != null ? `Cap ${formatCap(key, status.caps[key])}` : 'No cap'}>
            <div className="text-2xs text-tertiary">{fieldShort(key)}</div>
            <div className={`text-sm ${TONE_CLASS[readingTone(status, key)]}`}>
              {formatReading(key, status.readings[key])}
              {status.caps[key] != null && (
                <span className="text-xs text-tertiary font-normal"> / {formatCap(key, status.caps[key])}</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

const INPUTS: { key: PoolCapField; label: string; step: string; max?: number }[] = [
  { key: 'week_pct', label: 'Week %', step: '1', max: 100 },
  { key: 'day_pct', label: 'Day %', step: '0.1', max: 100 },
  { key: 'week_usd', label: 'Week $', step: '1' },
  { key: 'day_usd', label: 'Day $', step: '1' },
]

function toForm(caps: PoolCapFields): Record<PoolCapField, string> {
  return {
    week_pct: caps.week_pct == null ? '' : String(caps.week_pct),
    day_pct: caps.day_pct == null ? '' : String(caps.day_pct),
    week_usd: caps.week_usd == null ? '' : String(caps.week_usd),
    day_usd: caps.day_usd == null ? '' : String(caps.day_usd),
  }
}

export function PoolCapForm({ data, onSave, saving, error }: {
  data: PoolCapResponse
  onSave: (update: PoolCapUpdate) => void
  saving: boolean
  error?: string | null
}) {
  const [form, setForm] = useState(() => toForm(data.caps))
  const [onReached, setOnReached] = useState(data.on_reached)
  // A new server snapshot replaces the draft (in render, on the change).
  const [shown, setShown] = useState(data)
  if (shown !== data) {
    setShown(data)
    setForm(toForm(data.caps))
    setOnReached(data.on_reached)
  }

  const submit = () => {
    const update: PoolCapUpdate = { on_reached: onReached }
    for (const key of CAP_FIELDS) {
      const raw = form[key].trim()
      const n = raw === '' ? null : parseFloat(raw)
      update[key] = n == null || isNaN(n) ? null : n
    }
    onSave(update)
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-end gap-3">
        {INPUTS.map(inp => (
          <div key={inp.key} className="w-24">
            <div className="text-xs text-secondary mb-1">{inp.label}</div>
            <Input
              type="number" min="0" max={inp.max} step={inp.step} placeholder="No cap"
              aria-label={inp.label}
              value={form[inp.key]}
              onChange={e => setForm(f => ({ ...f, [inp.key]: e.target.value }))}
            />
          </div>
        ))}
        <div>
          <div className="text-xs text-secondary mb-1">When reached</div>
          <Select
            aria-label="When reached"
            value={onReached}
            onChange={e => setOnReached(e.target.value as 'stop' | 'continue')}
          >
            <option value="stop">Stop</option>
            <option value="continue">Continue on API keys</option>
          </Select>
        </div>
        <Button variant="primary" size="xs" onClick={submit} disabled={saving}>
          Save
        </Button>
      </div>
      <p className="text-xs text-tertiary">
        Empty means no cap. Less than 14.3% a day cannot use a whole week.
      </p>
      {error && <p className="text-xs text-status-danger-fg">{error}</p>}
    </div>
  )
}

export function PoolCapPanel({ title, intro, info, empty, data, isLoading, onSave, saving, error }: {
  title: string
  intro: string
  info: string
  empty: string
  data: PoolCapResponse | undefined
  isLoading: boolean
  onSave: (update: PoolCapUpdate) => void
  saving: boolean
  error?: string | null
}) {
  const engines = data ? Object.values(data.engines) : []
  return (
    <div className={`${CARD} overflow-hidden`}>
      <div className="px-4 py-3 border-b border-subtle">
        <div>
          <h3 className="inline text-sm font-medium text-primary">{title}</h3>
          <InfoTip label={title}>{info}</InfoTip>
        </div>
        <p className="text-xs text-tertiary mt-0.5">
          {intro}
        </p>
      </div>
      <div className="px-4 py-3 space-y-3">
        {isLoading && <div className="text-xs text-tertiary">Loading...</div>}
        {!isLoading && engines.length === 0 && (
          <div className="text-xs text-tertiary">{empty}</div>
        )}
        {engines.map(s => <EngineLine key={s.layer} status={s} />)}
        {data && (
          <div className="pt-3 border-t border-subtle">
            <PoolCapForm data={data} onSave={onSave} saving={saving} error={error} />
          </div>
        )}
      </div>
    </div>
  )
}

export function PlatformPoolCapSection() {
  const { data, isLoading } = useAdminPoolCap()
  const save = useSetAdminPoolCap()
  return (
    <PoolCapPanel
      title="Shared subscriptions"
      intro="You can cap the shared subscriptions of each engine. Cap them at a percentage of their weekly window, or at the matching API cost."
      info="The percentage covers all the shared subscriptions of each engine. Everyone's use of the accounts counts. The API cost counts agent work over the last 7 days and 24 hours. The first cap reached stops new agent work, or moves it onto an API key."
      empty="No subscription accounts are shared with agents."
      data={data} isLoading={isLoading}
      onSave={u => save.mutate(u)} saving={save.isPending}
      error={save.error ? save.error.message : null}
    />
  )
}

export function MyPoolCapSection() {
  const { data, isLoading } = useMyPoolCap()
  const save = useSetMyPoolCap()
  return (
    <PoolCapPanel
      title="My subscriptions"
      intro="You can cap your own connected accounts on each engine. Cap them at a percentage of their weekly window, or at the matching API cost."
      info="The percentage covers all the enabled subscriptions of each engine. Everything you do on the accounts counts. The API cost counts your chats over the last 7 days and 24 hours. The first cap reached stops your new chats, or moves them onto an API key."
      empty="No connected subscription accounts."
      data={data} isLoading={isLoading}
      onSave={u => save.mutate(u)} saving={save.isPending}
      error={save.error ? save.error.message : null}
    />
  )
}
