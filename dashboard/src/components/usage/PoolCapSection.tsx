import { useEffect, useState } from 'react'
import {
  PoolCapResponse, PoolCapStatus, PoolCapUpdate, PoolCapFields, PoolCapField,
  useMyPoolCap, useSetMyPoolCap, useAdminPoolCap, useSetAdminPoolCap,
} from '@/api/usage'
import { useExecutionLayers } from '@/api/agents'
import { engineLabels } from '@/lib/engines'
import {
  CAP_FIELDS, engineName, fieldShort, formatCap, formatReading, readingTone,
} from './poolCap'

// One subscription pool's readings per engine and its cap form. The
// platform pool (Setup → Usage) and each user's own accounts (User
// Settings → Usage) share this; the data hooks differ.

const TONE_CLASS = {
  error: 'text-p-error font-medium',
  warn: 'text-p-accent-yellow font-medium',
  plain: 'text-p-text-secondary',
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
        <span className="font-medium text-p-text">{engineName(status.layer, engineLabels(layers))}</span>
        <span className="text-xs text-p-text-light">
          {status.accounts} {status.accounts === 1 ? 'account' : 'accounts'}
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1.5">
        {CAP_FIELDS.map(key => (
          <div key={key} data-testid={`pool-reading-${key}`}
            title={status.caps[key] != null ? `cap ${formatCap(key, status.caps[key])}` : 'no cap'}>
            <div className="text-[11px] text-p-text-light">{fieldShort(key)}</div>
            <div className={`text-sm ${TONE_CLASS[readingTone(status, key)]}`}>
              {formatReading(key, status.readings[key])}
              {status.caps[key] != null && (
                <span className="text-xs text-p-text-light font-normal"> / {formatCap(key, status.caps[key])}</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// A small ⓘ that opens a one-paragraph note in place (a click works on a
// phone, where hover tooltips do not).
function InfoTip({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        aria-label="More about this cap"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        className="ml-1 inline-flex h-4 w-4 items-center justify-center rounded-full border border-p-border-light text-[10px] leading-none text-p-text-light hover:text-p-text align-text-bottom"
      >
        i
      </button>
      {open && <p className="text-xs text-p-text-light mt-1" data-testid="pool-cap-info">{text}</p>}
    </>
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
  useEffect(() => {
    setForm(toForm(data.caps))
    setOnReached(data.on_reached)
  }, [data])

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
          <div key={inp.key}>
            <div className="text-xs text-p-text-secondary mb-1">{inp.label}</div>
            <input
              type="number" min="0" max={inp.max} step={inp.step} placeholder="No cap"
              aria-label={inp.label}
              value={form[inp.key]}
              onChange={e => setForm(f => ({ ...f, [inp.key]: e.target.value }))}
              className="w-24 px-2 py-1 rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-sm text-p-text"
            />
          </div>
        ))}
        <div>
          <div className="text-xs text-p-text-secondary mb-1">When reached</div>
          <select
            aria-label="When reached"
            value={onReached}
            onChange={e => setOnReached(e.target.value as 'stop' | 'continue')}
            className="px-2 py-1 rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-sm text-p-text"
          >
            <option value="stop">Stop</option>
            <option value="continue">Continue on API keys</option>
          </select>
        </div>
        <button onClick={submit} disabled={saving}
          className="px-3 py-1.5 text-xs rounded-sm bg-brand text-white hover:bg-brand-hover disabled:opacity-50">
          Save
        </button>
      </div>
      <p className="text-xs text-p-text-light">
        Empty means no cap. Less than 14.3% a day cannot use a whole week.
      </p>
      {error && <p className="text-xs text-p-error">{error}</p>}
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
    <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface overflow-hidden">
      <div className="px-4 py-3 border-b border-p-border-light">
        <h3 className="text-sm font-medium text-p-text">{title}</h3>
        <p className="text-xs text-p-text-light mt-0.5">
          {intro}
          <InfoTip text={info} />
        </p>
      </div>
      <div className="px-4 py-3 space-y-3">
        {isLoading && <div className="text-xs text-p-text-light">Loading...</div>}
        {!isLoading && engines.length === 0 && (
          <div className="text-xs text-p-text-light">{empty}</div>
        )}
        {engines.map(s => <EngineLine key={s.layer} status={s} />)}
        {data && (
          <div className="pt-3 border-t border-p-border-light">
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
      title="Subscription pool"
      intro="You can cap the accounts in the agent pool on each engine to a percentage of their weekly window or to an equivalent API cost."
      info="The percentage is measured against all the pooled subscriptions of each engine, everyone's use of the accounts included. The API cost counts agent work over the last 7 days and 24 hours. The first cap reached stops new agent work, or moves it onto an API key."
      empty="No subscription accounts in the agent pool."
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
      intro="You can cap your own connected accounts on each engine to a percentage of their weekly window or to an equivalent API cost."
      info="The percentage is measured against all the enabled subscriptions of each engine, everything you do on the accounts included. The API cost counts your chats over the last 7 days and 24 hours. The first cap reached stops your new chats, or moves them onto an API key."
      empty="No connected subscription accounts."
      data={data} isLoading={isLoading}
      onSave={u => save.mutate(u)} saving={save.isPending}
      error={save.error ? save.error.message : null}
    />
  )
}
