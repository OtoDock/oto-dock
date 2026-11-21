/**
 * The Conversations tab's People view: everyone from outside who reached the
 * agent, each a collapsed card whose details open under it, one at a time.
 * The details show what the agent keeps about them (memory, files,
 * conversations, notes for the team), export it as a zip, and forget them
 * after a confirm. A conversation still running with them refuses the
 * forget, and the server's sentence says so.
 */

import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  usePeople, usePerson, useExportPerson, useForgetPerson, type OutsidePerson,
} from '../../api/outside'
import { errorText } from '../../lib/http/apiJson'
import { emitAppToast } from '../../lib/appToasts'
import { NOTIFICATION_SEVERITY } from '../../lib/status/notificationSeverity'
import { formatRelativeTime } from '../../lib/format'
import { formatBytes } from '../admin/PlatformPage.format'
import { useDialogs } from '../../components/ui/dialogs'
import Button from '../../components/ui/Button'
import CollapsedCard from '../../components/ui/CollapsedCard'
import InfoTip from '../../components/ui/InfoTip'
import { BADGE_BASE, BADGE_TONE, BUTTON_DANGER_QUIET } from '../../components/ui/classes'

const FOCUS_RING = 'rounded-control focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring'

/** When, in words; a dash when the date is missing. */
function seen(iso: string | null | undefined): string {
  return iso ? formatRelativeTime(iso) : '—'
}

function numberOf(p: { number: string }): string {
  return p.number || 'Unknown number'
}

function PinBadge() {
  return (
    <span className={`${BADGE_BASE} ${BADGE_TONE.success}`} title="This person entered the line's PIN.">PIN</span>
  )
}

function OneLine({ text }: { text: string }) {
  if (!text) return <span className="text-tertiary">—</span>
  return <span className="block truncate" title={text}>{text}</span>
}

function PanelSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h2 className="text-xs font-semibold text-secondary uppercase">{title}</h2>
      {children}
    </section>
  )
}

function PersonDetails({ agent, id, onForgotten }: { agent: string; id: string; onForgotten: () => void }) {
  const navigate = useNavigate()
  const dialogs = useDialogs()
  const { data, isLoading, error: loadError } = usePerson(agent, id)
  const exportPerson = useExportPerson(agent)
  const forget = useForgetPerson(agent)
  const [error, setError] = useState('')

  const onExport = () => {
    setError('')
    exportPerson.mutateAsync(id).catch((e) => setError(errorText(e) || 'The export was not prepared.'))
  }

  const onForget = async () => {
    if (!(await dialogs.confirm({
      title: `Forget ${numberOf(data ?? { number: '' })}?`,
      body: 'Their memory, files and conversations on this agent are deleted. Their notes for the team and their call history go too. This cannot be undone.',
      confirmLabel: 'Forget',
      cancelLabel: 'Cancel',
      tone: 'danger',
    }))) return
    setError('')
    try {
      await forget.mutateAsync(id)
    } catch (e) {
      setError(errorText(e) || 'This person was not forgotten.')
      return
    }
    emitAppToast({ title: 'This person was forgotten.', body: '', severity: NOTIFICATION_SEVERITY.SUCCESS })
    onForgotten()
  }

  return (
    <div className="space-y-4">
      {isLoading && <p className="text-sm text-tertiary">Loading…</p>}
      {loadError && <p className="text-sm text-status-danger-fg">{errorText(loadError) || 'This person could not be loaded.'}</p>}
      {data && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-tertiary">
              {`First seen ${seen(data.first_seen)}. Last seen ${seen(data.last_seen)}.`}
            </p>
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center">
                <Button size="xs" onClick={onExport} disabled={exportPerson.isPending}>Export</Button>
                <InfoTip label="Export">A zip of what this agent keeps about this person.</InfoTip>
              </span>
              <button
                type="button"
                className={BUTTON_DANGER_QUIET}
                onClick={() => { void onForget() }}
                disabled={forget.isPending}
              >
                Forget
              </button>
            </div>
          </div>

          {error && <p role="alert" className="text-xs text-status-danger-fg">{error}</p>}

          <PanelSection title="Memory">
            {data.memory.length === 0 && <p className="text-xs text-tertiary">Nothing kept yet.</p>}
            {data.memory.map((m) => (
              <div key={m.name} className="space-y-1">
                <p className="text-xs font-medium text-primary font-mono">{m.name}</p>
                <pre className="text-xs text-secondary whitespace-pre-wrap break-words rounded-control bg-surface-sunken p-2 max-h-64 overflow-auto">{m.text}</pre>
              </div>
            ))}
          </PanelSection>

          <PanelSection title="Files">
            {data.files.length === 0 && <p className="text-xs text-tertiary">No files.</p>}
            {data.files.length > 0 && (
              <ul className="space-y-0.5">
                {data.files.map((f) => (
                  <li key={f.path} className="flex items-baseline justify-between gap-3 text-xs">
                    <span className="font-mono text-primary break-all">{f.path}</span>
                    <span className="shrink-0 text-tertiary">{formatBytes(f.size)}</span>
                  </li>
                ))}
              </ul>
            )}
          </PanelSection>

          <PanelSection title="Conversations">
            {data.conversations.length === 0 && <p className="text-xs text-tertiary">No conversations.</p>}
            {data.conversations.length > 0 && (
              <ul className="space-y-1">
                {data.conversations.map((c) => (
                  <li key={c.id}>
                    <button
                      type="button"
                      onClick={() => navigate(`/conversations/${c.id}`)}
                      className={`w-full flex items-baseline gap-3 px-2 py-1.5 text-left text-xs hover:bg-surface-hover ${FOCUS_RING}`}
                    >
                      <span className="shrink-0 text-secondary">{c.started ? new Date(c.started).toLocaleString() : '—'}</span>
                      <span className="min-w-0 flex-1 text-primary"><OneLine text={c.outcome} /></span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </PanelSection>

          {data.notes.length > 0 && (
            <PanelSection title="Notes for the team">
              <ul className="space-y-0.5">
                {data.notes.map((n, i) => (
                  <li key={i} className="text-xs text-secondary">
                    <span className="text-tertiary">{n.day}</span> {n.text}
                  </li>
                ))}
              </ul>
            </PanelSection>
          )}
        </>
      )}
    </div>
  )
}

function PersonCard({ agent, person, open, onToggle, onForgotten }: {
  agent: string
  person: OutsidePerson
  open: boolean
  onToggle: () => void
  onForgotten: () => void
}) {
  return (
    <CollapsedCard
      open={open}
      onToggle={onToggle}
      keepInView
      buttonClassName="px-4 py-3"
      bodyClassName="px-4 pb-4 pt-1"
      header={(
        <span className="flex items-center gap-2 min-w-0">
          <span className="text-sm font-medium text-primary font-mono truncate">{numberOf(person)}</span>
          {person.verified && <PinBadge />}
        </span>
      )}
      preview={(
        <span className="block mt-1 space-y-1">
          {person.last_outcome && <span className="block text-xs text-secondary truncate">{person.last_outcome}</span>}
          <span className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-tertiary">
            <span>{person.conversations === 1 ? '1 conversation' : `${person.conversations} conversations`}</span>
            <span>{`Last seen ${seen(person.last_seen)}`}</span>
          </span>
        </span>
      )}
    >
      <PersonDetails agent={agent} id={person.id} onForgotten={onForgotten} />
    </CollapsedCard>
  )
}

export function PeopleView({ agent, selected, onSelect }: {
  agent: string
  /** The open person's id, or ''. */
  selected: string
  onSelect: (id: string) => void
}) {
  const { data: people, isLoading, error } = usePeople(agent)
  const list: OutsidePerson[] = people ?? []
  const toggle = (id: string) => onSelect(selected === id ? '' : id)

  return (
    <>
      {isLoading && <div className="text-center py-12 text-tertiary">Loading...</div>}
      {error && <p className="text-sm text-status-danger-fg">{errorText(error)}</p>}
      {!isLoading && !error && list.length === 0 && (
        <div className="text-center py-12 text-tertiary">Nobody from outside has reached this agent yet.</div>
      )}

      {list.length > 0 && (
        <ul className="space-y-3">
          {list.map((p) => (
            <li key={p.id}>
              <PersonCard
                agent={agent}
                person={p}
                open={selected === p.id}
                onToggle={() => toggle(p.id)}
                onForgotten={() => onSelect('')}
              />
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
