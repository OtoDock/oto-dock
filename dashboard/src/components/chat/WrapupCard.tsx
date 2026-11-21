import type { ReactNode } from 'react'

import { WRAPUP_STATE, WROTE_KIND } from '../../lib/status/wrapup'

// The wrap-up after a call (proxy `services/phone/wrapup.py`): one card under
// the phone conversation, written when the wrap-up ends. A line for one that
// had nothing to write or that did not finish, then the outcome, the follow-ups
// and what it wrote. Tokens only, so it reads in both themes.

const STATE_LINE: Record<string, string> = {
  [WRAPUP_STATE.SKIPPED]: 'Nobody spoke on this call.',
  [WRAPUP_STATE.FAILED]: 'The wrap-up did not finish.',
}

const WROTE_LABEL: Record<string, string> = {
  [WROTE_KIND.MEMORY]: 'Memory',
  [WROTE_KIND.FILE]: 'File',
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="mt-2">
      <p className="text-3xs font-semibold uppercase tracking-wide text-tertiary">{title}</p>
      {children}
    </div>
  )
}

export default function WrapupCard({ state, outcome, followUps, wrote }: {
  state: string
  outcome: string
  followUps: string[]
  wrote: Array<{ kind: string; name: string }>
}) {
  const line = STATE_LINE[state] ?? ''
  const failed = state === WRAPUP_STATE.FAILED
  // A failure's reason is often the line itself: it is said once.
  const shownOutcome = outcome.trim() && outcome.trim() !== line ? outcome.trim() : ''
  return (
    <section
      aria-label="After the call"
      data-testid="wrapup-card"
      className="my-1.5 rounded-card border border-subtle bg-surface-muted px-3 py-2 text-xs text-primary"
    >
      <p className="text-sm font-medium">After the call</p>
      {line && <p className={`mt-0.5 ${failed ? 'text-status-warning-fg' : 'text-secondary'}`}>{line}</p>}
      {shownOutcome && (
        <Section title="Outcome">
          <p className="mt-0.5 whitespace-pre-wrap break-words">{shownOutcome}</p>
        </Section>
      )}
      {followUps.length > 0 && (
        <Section title="Follow-ups">
          <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
            {followUps.map((f, i) => <li key={i} className="break-words">{f}</li>)}
          </ul>
        </Section>
      )}
      {wrote.length > 0 && (
        <Section title="Written">
          <ul className="mt-0.5 space-y-0.5">
            {wrote.map((w, i) => (
              <li key={i} className="break-words">
                {WROTE_LABEL[w.kind] ?? 'File'}: <span className="font-mono">{w.name}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </section>
  )
}
