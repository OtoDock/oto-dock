/**
 * An OAuth account's usage windows as the vendor reports them: the session
 * window, the weekly window and any per-model weekly window, each as a
 * compact bar with its reset instant. Rendered on the account rows of both
 * AI Engines cards (Setup and User Settings) from the same `windows` field.
 */

import type { SubscriptionWindows as Windows } from '../../api/executionLayers'

function fillClass(pct: number): string {
  return pct >= 100 ? 'bg-p-error' : pct >= 80 ? 'bg-p-accent-yellow' : 'bg-brand'
}

function textClass(pct: number): string {
  return pct >= 100 ? 'text-p-error' : pct >= 80 ? 'text-p-accent-yellow' : 'text-p-text-light'
}

/** "14:00" for today, "Wed 09:00" otherwise, in the viewer's clock. */
export function formatReset(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (d.toDateString() === now.toDateString()) return time
  return `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${time}`
}

function formatAge(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60000))
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`
}

/** The full instant for the hover title, e.g. "Wed, 16 Sep, 09:00". */
function formatResetLong(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString(undefined, {
    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  })
}

function WindowBar({ name, pct, resetsAt, active }: {
  name: string
  pct: number
  resetsAt: string | null
  active?: boolean
}) {
  const shown = Math.min(Math.max(pct, 0), 100)
  const when = formatReset(resetsAt)
  // "until" only when the window is really full: the vendor's `active`
  // flag marks the limit to watch (it sits on a 61 % window once the
  // session window has reset), not a window the vendor refuses.
  const full = pct >= 100
  void active
  // Label, bar and percent share one line; the reset instant follows on
  // the same line where there is room and drops to its own line, indented
  // under the bar, on a phone — a truncated "resets W…" said nothing.
  return (
    <div className="flex flex-wrap items-center gap-x-2 text-xs" data-testid="window-bar">
      <span className="w-14 shrink-0 text-p-text-light">{name}</span>
      <div className="h-1.5 w-24 shrink-0 rounded-full bg-p-surface overflow-hidden">
        <div className={`h-full rounded-full ${fillClass(pct)}`} style={{ width: `${shown}%` }} />
      </div>
      <span className={`font-medium ${textClass(pct)}`}>{Math.round(pct)}%</span>
      {when && (
        <span
          className="text-p-text-light basis-full pl-16 sm:basis-auto sm:pl-0"
          title={formatResetLong(resetsAt)}
        >
          {full ? `until ${when}` : `resets ${when}`}
        </span>
      )}
    </div>
  )
}

export function SubscriptionWindowBars({ windows }: { windows: Windows | null | undefined }) {
  if (windows === undefined) return null
  if (windows === null) {
    return <p className="mt-1 text-xs text-p-text-light/70">Usage not read yet.</p>
  }
  const hasAny = windows.five_hour || windows.seven_day || windows.scoped.length > 0
  if (!hasAny) return null
  return (
    <div className="mt-1.5 space-y-0.5" data-testid="subscription-windows">
      {windows.five_hour && (
        <WindowBar name="Session" pct={windows.five_hour.pct} resetsAt={windows.five_hour.resets_at} />
      )}
      {windows.seven_day && (
        <WindowBar name="Week" pct={windows.seven_day.pct} resetsAt={windows.seven_day.resets_at} />
      )}
      {windows.scoped.map((s) => (
        <WindowBar key={s.key} name={s.label} pct={s.pct} resetsAt={s.resets_at} active={s.active} />
      ))}
      <p className="text-[11px] text-p-text-light/70">as of {formatAge(windows.observed_at)}</p>
    </div>
  )
}

/** Shown under an account list that holds more than one OAuth account: the
 *  pool balances them itself (the account that resets first is used first),
 *  so benching accounts by hand only loses quota. */
export function BalanceHint({ oauthCount }: { oauthCount: number }) {
  if (oauthCount < 2) return null
  return (
    <p className="text-xs text-p-text-light" data-testid="balance-hint">
      Enable all your subscriptions to balance the load automatically.
    </p>
  )
}
