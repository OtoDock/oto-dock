/**
 * "OtoDock was updated to X": a dialog every signed-in person sees once per
 * version, on the first dashboard load after the install changed release.
 * GET /v1/users/me/release-card decides who is eligible (an admin after any
 * upgrade; a member alike, but shown only when the running version's
 * highlights carry something for everyone; nobody on a fresh install, a
 * -dev tree or a version already seen). The cards come from the curated
 * highlights file bundled with the dashboard (release/highlights.json): an
 * admin sees both lists and a "Release notes" button, a member the list for
 * everyone and no link; no cards for the viewer: one line. A downgrade keeps
 * its own headline with no cards. Escape, the backdrop and Close dismiss it
 * and mark the shown version seen server-side (one atomic merge on the prefs
 * row), so a dialog left open across an upgrade marks the old version; a
 * version the viewer need not see (eligible with nothing to show, or a fresh
 * install's first load) is marked silently once, so a later release is
 * measured from this one. The dialog closes the moment it is dismissed,
 * whatever the mark answers: a failed mark brings it back on the next load,
 * never a stuck overlay.
 */

import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../api/auth'
import { useAuth } from '../contexts/AuthContext'
import { ICON_PATHS, highlightsFor, type Highlight, type IconName } from '../release/highlights'

export interface ReleaseCard {
  show_admin: boolean
  eligible: boolean
  version: string
  previous: string
  direction: 'up' | 'down' | string
  seen: string
  release_url: string
}

export const RELEASE_CARD_KEY = ['release-card']

/** The cards the viewer sees: an admin both lists, a member the list for everyone; a downgrade none. */
export function cardsFor(card: ReleaseCard): { everyone: Highlight[]; admins: Highlight[] } {
  if (card.direction === 'down') return { everyone: [], admins: [] }
  const h = highlightsFor(card.version)
  return { everyone: h.everyone, admins: card.show_admin ? h.admins : [] }
}

/** Whether the dialog opens: the server's eligibility, then for a member the file. */
export function shouldShow(card: ReleaseCard): boolean {
  if (!card.eligible) return false
  if (card.show_admin) return true
  return cardsFor(card).everyone.length > 0
}

async function markSeen(version: string): Promise<void> {
  const res = await apiFetch('/v1/users/me/release-card/seen', { method: 'POST', body: JSON.stringify({ version }) })
  if (!res.ok) throw new Error('Failed to mark the release seen')
}

function Icon({ name }: { name: string }) {
  const d = ICON_PATHS[name as IconName] ?? ICON_PATHS.sparkle
  return (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d={d} />
    </svg>
  )
}

function Cards({ items }: { items: Highlight[] }) {
  return (
    <ul className="space-y-3">
      {items.map((h, i) => (
        <li key={i} className="flex items-start gap-3">
          <span className="w-9 h-9 rounded-full bg-brand/10 text-brand flex items-center justify-center shrink-0">
            <Icon name={h.icon} />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-p-text">{h.title}</p>
            <p className="text-sm text-p-text-secondary">{h.text}</p>
          </div>
        </li>
      ))}
    </ul>
  )
}

export function ReleaseDialog() {
  const { user } = useAuth()
  const qc = useQueryClient()
  const markedFor = useRef('')
  const [dismissedFor, setDismissedFor] = useState('')
  const { data } = useQuery({
    queryKey: RELEASE_CARD_KEY,
    queryFn: async (): Promise<ReleaseCard> => {
      const res = await apiFetch('/v1/users/me/release-card')
      if (!res.ok) throw new Error('Failed to fetch')
      return res.json()
    },
    enabled: !!user,
    staleTime: 60 * 60_000,
  })
  const show = !!data && shouldShow(data) && dismissedFor !== data.version

  // Nothing to show for a version not yet marked: an eligible member with
  // nothing for everyone (or on a downgrade), or a person with no seen
  // version at all (a fresh install, a new person). A person the server
  // leaves out with a seen version (a -dev tree, an unknown version) is left
  // alone.
  useEffect(() => {
    if (!data || show || !data.version || data.seen === data.version || markedFor.current === data.version) return
    if (!data.eligible && data.seen) return
    markedFor.current = data.version
    void markSeen(data.version)
      .then(() => qc.setQueryData<ReleaseCard>(RELEASE_CARD_KEY, (old) => (old ? { ...old, seen: old.version } : old)))
      .catch(() => { markedFor.current = '' })
  }, [data, show, qc])

  const dismiss = () => {
    if (!data) return
    // The silent-mark effect must not post a second time once the dialog hides.
    markedFor.current = data.version
    setDismissedFor(data.version)
    void markSeen(data.version)
      .then(() => qc.invalidateQueries({ queryKey: RELEASE_CARD_KEY }))
      .catch(() => { /* hidden for this load; the next load asks again */ })
  }

  useEffect(() => {
    if (!show) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') dismiss() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [show, data?.version])

  if (!data || !show) return null

  const down = data.direction === 'down'
  const headline = down
    ? `OtoDock now runs ${data.version}${data.previous ? ` (it ran ${data.previous} before)` : ''}.`
    : `OtoDock was updated to ${data.version}${data.previous ? ` (from ${data.previous})` : ''}.`
  const cards = cardsFor(data)
  const both = cards.everyone.length > 0 && cards.admins.length > 0
  const none = cards.everyone.length === 0 && cards.admins.length === 0

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={dismiss}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="release-dialog-title"
        className="bg-white dark:bg-p-surface rounded-2xl shadow-xl border border-p-border dark:border-gray-700 w-full max-w-lg overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="bg-brand-surface px-5 py-4 border-b border-brand/20">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-brand">What is new</p>
          <h2 id="release-dialog-title" className="text-lg font-semibold text-p-text mt-0.5">{headline}</h2>
        </div>
        {!down && (
          <div className="px-5 py-4 max-h-[60vh] overflow-y-auto space-y-4">
            {none && <p className="text-sm text-p-text-secondary">Fixes and polish throughout this release.</p>}
            {cards.everyone.length > 0 && (
              <section>
                {both && <p className="text-[11px] font-semibold uppercase tracking-wider text-p-text-light mb-2">For everyone</p>}
                <Cards items={cards.everyone} />
              </section>
            )}
            {cards.admins.length > 0 && (
              <section>
                {both && <p className="text-[11px] font-semibold uppercase tracking-wider text-p-text-light mb-2">For admins</p>}
                <Cards items={cards.admins} />
              </section>
            )}
          </div>
        )}
        <div className="px-5 py-3 border-t border-p-border-light flex items-center justify-end gap-2">
          {data.show_admin && !down && data.release_url && (
            <a
              href={data.release_url}
              target="_blank"
              rel="noopener noreferrer"
              className="px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors"
            >
              Release notes
            </a>
          )}
          <button
            type="button"
            autoFocus
            onClick={dismiss}
            className="px-3 py-1.5 text-sm font-medium rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
