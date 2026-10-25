import { useState } from 'react'
import { SHARE_TARGET, type ShareInboxItem } from '../../api/shares'
import type { AgentSummary } from '../../api/agents'
import { ROLE, canEditAgent, roleLabel } from '../../lib/permissions'
import { isSharedOnly, modeOfAgent } from '../../lib/visibility'
import { useAuth } from '../../contexts/AuthContext'
import SwipeableRow from '../chat/notifications/SwipeableRow'
import Button from '../ui/Button'

/**
 * "Shared with you" (SHARING.md): the block at the top of the notifications
 * panel. An item renders by the actions the server gave it: one that takes a
 * decision (an app waiting to be placed) shows Accept with a select of the
 * person's agents and Decline, and no x or swipe; a received one (an accepted
 * app, a chat) opens from its arrow and goes away with the x or a swipe. The
 * panel feeds it by props; the data hook and the handlers live in
 * useChatNotifications.
 */

export interface ShareInboxProps {
  items: ShareInboxItem[]
  /** The person's agents, for the Accept select (the app's own agent is left out). */
  agents?: AgentSummary[]
  busyId?: string
  error?: string
  onAccept: (item: ShareInboxItem, agent: string) => void
  onDecline: (item: ShareInboxItem) => void
  onHide: (item: ShareInboxItem) => void
  onOpen: (item: ShareInboxItem) => void
}

const takesDecision = (item: ShareInboxItem) => item.actions.includes('accept')

function fromLine(item: ShareInboxItem): string {
  const who = item.shared_by_name || 'a colleague'
  return item.agent_name ? `from ${who} · ${item.agent_name}` : `from ${who}`
}

function DecisionRow({ item, agents, busy, onAccept, onDecline }: {
  item: ShareInboxItem; agents: AgentSummary[]; busy: boolean
  onAccept: (agent: string) => void; onDecline: () => void
}) {
  const user = useAuth()?.user ?? null
  // A Shared-only agent's chats all run as the agent, so an app accepted
  // there goes to everyone on it (the agent's placement), which takes the
  // editor role: below it the agent is not offered, and at it the row says
  // what Accept does (SHARING.md "Accepting into a Shared-only agent").
  const sharedOnly = (a: AgentSummary) => isSharedOnly(modeOfAgent(a))
  const choices = agents.filter((a) => a.name !== item.agent && (!sharedOnly(a) || canEditAgent(user, a.name)))
  const [agent, setAgent] = useState(() => choices[0]?.name ?? '')
  const pick = choices.some((a) => a.name === agent) ? agent : (choices[0]?.name ?? '')
  const picked = choices.find((a) => a.name === pick)
  const forEveryone = !!picked && sharedOnly(picked)
  return (
    <div className="border-l-[3px] border-l-action px-3 py-2.5" data-testid="share-inbox-decision">
      <div className="text-sm font-medium text-primary truncate">{item.title || 'An app'}</div>
      <div className="text-2xs text-tertiary">
        {fromLine(item)}
        {item.role_cap !== ROLE.VIEWER && <> · as {roleLabel(item.role_cap).toLowerCase()}</>}
      </div>
      <div className="mt-1.5 flex items-center gap-1.5">
        {choices.length > 0 ? (
          <>
            {/* A native select: the panel closes on a press outside it, and a
                portaled list would count as outside. */}
            <select value={pick} onChange={(e) => setAgent(e.target.value)} aria-label="Place in"
              className="min-w-0 flex-1 rounded-control border border-strong bg-surface-card px-1.5 py-1 text-xs text-primary focus:border-action focus:outline-none">
              {choices.map((a) => (
                <option key={a.name} value={a.name}>
                  {(a.display_name || a.name) + (sharedOnly(a) ? ', for everyone there' : '')}
                </option>
              ))}
            </select>
            <Button size="xs" variant="primary" disabled={busy || !pick} onClick={() => onAccept(pick)}>
              {forEveryone ? 'Add for everyone' : 'Accept'}
            </Button>
          </>
        ) : (
          <span className="min-w-0 flex-1 text-2xs text-tertiary">Join an agent to place it. It opens from its page meanwhile.</span>
        )}
        <Button size="xs" disabled={busy} onClick={onDecline}>
          Decline
        </Button>
      </div>
      {forEveryone && (
        <p className="mt-1 text-2xs text-tertiary" data-testid="share-inbox-for-everyone">
          {picked.display_name || picked.name} is Shared only, so its chats run as the agent. The app goes
          to its apps for everyone there. {item.shared_by_name || 'The person who shared it'} is told.
        </p>
      )}
    </div>
  )
}

export default function ShareInboxSection({ items, agents = [], busyId, error, onAccept, onDecline, onHide, onOpen }: ShareInboxProps) {
  if (!items.length) return null
  const pending = items.filter(takesDecision)
  const received = items.filter((i) => !takesDecision(i))
  return (
    <div data-testid="share-inbox">
      <div className="px-3 py-1.5 text-2xs font-medium uppercase tracking-wider text-link bg-action-subtle">
        Shared with you
      </div>
      {error && <p className="px-3 py-1 text-2xs text-status-danger-fg" role="alert">{error}</p>}
      {pending.map((item) => (
        <DecisionRow key={item.id} item={item} agents={agents} busy={busyId === item.id}
          onAccept={(agent) => onAccept(item, agent)} onDecline={() => onDecline(item)} />
      ))}
      {received.map((item) => (
        <SwipeableRow key={item.id} onSwipeDismiss={() => onHide(item)} resetKey={error}>
          <div className="flex items-start gap-2 border-l-[3px] border-l-action/50 px-3 py-2.5 hover:bg-surface-hover"
            data-testid="share-inbox-received">
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-primary truncate">{item.title || (item.kind === SHARE_TARGET.APP ? 'An app' : 'A chat')}</div>
              <div className="text-2xs text-tertiary">
                {fromLine(item)}
                {item.placed_agent && <> · in {agents.find((a) => a.name === item.placed_agent)?.display_name || item.placed_agent}</>}
              </div>
            </div>
            <div className="mt-0.5 flex shrink-0 flex-col items-center gap-1">
              <button type="button" onClick={() => onHide(item)} title="Hide"
                className="flex h-5 w-5 items-center justify-center text-tertiary transition-colors hover:text-secondary">
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
              <button type="button" onClick={() => onOpen(item)} title="Open"
                className="flex h-5 w-5 items-center justify-center text-tertiary transition-colors hover:text-link">
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7l5 5m0 0l-5 5m5-5H6" />
                </svg>
              </button>
            </div>
          </div>
        </SwipeableRow>
      ))}
    </div>
  )
}
