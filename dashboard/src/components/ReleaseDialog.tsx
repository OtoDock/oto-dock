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
import Dialog from './ui/Dialog'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../contexts/AuthContext'
import { ICON_PATHS, highlightsFor, type Highlight, type IconName } from '../release/highlights'
import { apiJson, apiOk, withMessage } from '../lib/http/apiJson'
import Button from './ui/Button'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT, DIALOG_PANEL } from './ui/classes'

export interface ReleaseCard {
  show_admin: boolean
  eligible: boolean
  version: string
  previous: string
  direction: 'up' | 'down' | string
  seen: string
  release_url: string
}

const RELEASE_CARD_KEY = ['release-card']

/** The cards the viewer sees: an admin both lists, a member the list for everyone; a downgrade none. */
function cardsFor(card: ReleaseCard): { everyone: Highlight[]; admins: Highlight[] } {
  if (card.direction === 'down') return { everyone: [], admins: [] }
  const h = highlightsFor(card.version)
  return { everyone: h.everyone, admins: card.show_admin ? h.admins : [] }
}

/** Whether the dialog opens: the server's eligibility, then for a member the file. */
function shouldShow(card: ReleaseCard): boolean {
  if (!card.eligible) return false
  if (card.show_admin) return true
  return cardsFor(card).everyone.length > 0
}

async function markSeen(version: string): Promise<void> {
  await apiOk('/v1/users/me/release-card/seen', { method: 'POST', body: JSON.stringify({ version }) })
    .catch(withMessage('Failed to mark the release seen'))
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
          <span className="w-9 h-9 rounded-full bg-surface-muted text-secondary flex items-center justify-center shrink-0">
            <Icon name={h.icon} />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-primary">{h.title}</p>
            <p className="text-sm text-secondary">{h.text}</p>
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
      return apiJson<ReleaseCard>('/v1/users/me/release-card').catch(withMessage('Failed to fetch'))
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

  if (!data || !show) return null

  const down = data.direction === 'down'
  const headline = down
    ? `OtoDock now runs ${data.version}.${data.previous ? ` It ran ${data.previous} before.` : ''}`
    : `OtoDock was updated${data.previous ? ` from ${data.previous}` : ''} to ${data.version}.`
  const cards = cardsFor(data)
  const both = cards.everyone.length > 0 && cards.admins.length > 0
  const none = cards.everyone.length === 0 && cards.admins.length === 0

  return (
    <Dialog
      onClose={dismiss}
      aria-labelledby="release-dialog-title"
      panelClassName={`${DIALOG_PANEL} max-w-lg overflow-hidden`}
    >
        <div className="bg-status-info-bg px-5 py-4 border-b border-subtle">
          <p className="text-2xs font-semibold uppercase tracking-wider text-status-info-fg">What is new</p>
          <h2 id="release-dialog-title" className="font-display text-lg font-semibold text-primary mt-0.5">{headline}</h2>
        </div>
        {!down && (
          <div className="px-5 py-4 max-h-[60vh] overflow-y-auto space-y-4">
            {none && <p className="text-sm text-secondary">Fixes and polish throughout this release.</p>}
            {cards.everyone.length > 0 && (
              <section>
                {both && <p className="text-2xs font-semibold uppercase tracking-wider text-tertiary mb-2">For everyone</p>}
                <Cards items={cards.everyone} />
              </section>
            )}
            {cards.admins.length > 0 && (
              <section>
                {both && <p className="text-2xs font-semibold uppercase tracking-wider text-tertiary mb-2">For admins</p>}
                <Cards items={cards.admins} />
              </section>
            )}
          </div>
        )}
        <div className="px-5 py-3 border-t border-subtle flex items-center justify-end gap-2">
          {data.show_admin && !down && data.release_url && (
            <a
              href={data.release_url}
              target="_blank"
              rel="noopener noreferrer"
              className={`${BUTTON_BASE} ${BUTTON_VARIANT.secondary} ${BUTTON_SIZE.sm}`}
            >
              Release notes
            </a>
          )}
          <Button variant="primary" type="button" autoFocus onClick={dismiss}>
            Close
          </Button>
        </div>
    </Dialog>
  )
}
