/**
 * Agent Settings → Conversations: the phone conversations of outside people
 * and of the agent's own line, and the people who reached the agent, for its
 * editors and managers. A two-way switch picks the view; the view and the
 * open person ride the address, so a conversation opened from here comes
 * back to the same place. A person's own calls live in their chat history.
 */

import { useState } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useAgentConversations } from '../../api/chats'
import { formatRelativeTime } from '../../lib/format'
import { SOURCE_TYPE } from '../../lib/session/kind'
import Button from '../../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, BUTTON_BASE, BUTTON_SIZE, CARD, SEGMENT } from '../../components/ui/classes'
import { PeopleView } from './AgentConversations.people'

const VIEW = { CONVERSATIONS: 'conversations', PEOPLE: 'people' } as const
type View = (typeof VIEW)[keyof typeof VIEW]

const VIEW_PARAM = 'view'
const PERSON_PARAM = 'person'
const PAGE_SIZE = 25

function SourceBadge({ source }: { source?: string }) {
  switch (source) {
    case SOURCE_TYPE.PHONE:
      return (
        <span className={`${BADGE_BASE} ${BADGE_TONE.media}`}>
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z" /></svg>
          Phone
        </span>
      )
    case SOURCE_TYPE.TASK:
      return (
        <span className={`${BADGE_BASE} ${BADGE_TONE.agent}`}>
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
          Task
        </span>
      )
    default:
      return (
        <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" /></svg>
          Chat
        </span>
      )
  }
}

/** The outcome on one line, a muted dash when there is none. */
function Outcome({ text }: { text: string }) {
  if (!text) return <span className="text-tertiary">—</span>
  return <span className="block truncate" title={text}>{text}</span>
}

function ConversationsView({ agent }: { agent: string }) {
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const { data, isLoading } = useAgentConversations(agent, offset, PAGE_SIZE)
  const conversations = data?.conversations ?? []
  const total = data?.total ?? 0
  const titleOf = (c: { title: string; source_type?: string }) =>
    c.title || (c.source_type === SOURCE_TYPE.PHONE ? 'Phone Call' : 'New Chat')

  return (
    <>
      {total > 0 && <p className="text-sm text-secondary text-right">{total} total</p>}

      {isLoading && (
        <div className="text-center py-12 text-tertiary">Loading...</div>
      )}

      {!isLoading && conversations.length === 0 && (
        <div className="text-center py-12 text-tertiary">
          No conversations with outside people yet. A person's own calls stay in their chat history.
        </div>
      )}

      {conversations.length > 0 && (
        <>
          {/* Desktop table */}
          <div className={`hidden md:block ${CARD} overflow-hidden`}>
            <table className="w-full text-sm table-fixed">
              <thead>
                <tr className="bg-surface-sunken text-left text-xs text-secondary uppercase tracking-wide border-b border-subtle">
                  <th className="px-4 py-3 w-1/4">Title</th>
                  <th className="px-4 py-3">Source</th>
                  <th className="px-4 py-3 w-1/4">Outcome</th>
                  <th className="px-4 py-3">Cost</th>
                  <th className="px-4 py-3">Started</th>
                  <th className="px-4 py-3">Last Updated</th>
                </tr>
              </thead>
              <tbody>
                {conversations.map(c => (
                  <tr
                    key={c.id}
                    onClick={() => navigate(`/conversations/${c.id}`)}
                    className="border-b border-subtle last:border-0 hover:bg-surface-hover cursor-pointer transition-colors"
                  >
                    <td className="px-4 py-3 text-primary truncate">{titleOf(c)}</td>
                    <td className="px-4 py-3">
                      <SourceBadge source={c.source_type} />
                    </td>
                    <td className="px-4 py-3 text-secondary">
                      <Outcome text={c.outcome ?? ''} />
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-secondary">
                      {(c.total_cost ?? 0) > 0 ? `$${(c.total_cost ?? 0).toFixed(4)}` : '—'}
                    </td>
                    <td className="px-4 py-3 text-secondary">
                      {formatRelativeTime(c.created_at)}
                    </td>
                    <td className="px-4 py-3 text-secondary">
                      {formatRelativeTime(c.updated_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Mobile cards */}
          <div className="md:hidden space-y-3">
            {conversations.map(c => (
              <div
                key={c.id}
                onClick={() => navigate(`/conversations/${c.id}`)}
                className={`${CARD} p-4 cursor-pointer hover:shadow-elevation-2 transition-shadow`}
              >
                <div className="flex items-start justify-between mb-2">
                  <p className="text-sm font-medium text-primary line-clamp-1">{titleOf(c)}</p>
                  <SourceBadge source={c.source_type} />
                </div>
                {c.outcome && <p className="text-xs text-secondary truncate mb-1" title={c.outcome}>{c.outcome}</p>}
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary">
                  <span>{formatRelativeTime(c.created_at)}</span>
                  {(c.total_cost ?? 0) > 0 && (
                    <span className="font-mono">${(c.total_cost ?? 0).toFixed(4)}</span>
                  )}
                </div>
              </div>
            ))}
          </div>

          {/* Pagination */}
          {total > PAGE_SIZE && (
            <div className="flex items-center justify-center gap-3 text-sm text-secondary">
              <Button
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </Button>
              <span>
                {offset + 1}&ndash;{Math.min(offset + PAGE_SIZE, total)} of {total}
              </span>
              <Button
                disabled={offset + PAGE_SIZE >= total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </Button>
            </div>
          )}
        </>
      )}
    </>
  )
}

export default function AgentConversations() {
  const { name } = useParams<{ name: string }>()
  const [params, setParams] = useSearchParams()
  const view: View = params.get(VIEW_PARAM) === VIEW.PEOPLE ? VIEW.PEOPLE : VIEW.CONVERSATIONS
  const person = view === VIEW.PEOPLE ? params.get(PERSON_PARAM) ?? '' : ''

  if (!name) return null

  const pickView = (next: View) => setParams((p) => {
    if (next === VIEW.PEOPLE) p.set(VIEW_PARAM, next)
    else p.delete(VIEW_PARAM)
    p.delete(PERSON_PARAM)
    return p
  }, { replace: true })
  const pickPerson = (id: string) => setParams((p) => {
    if (id) p.set(PERSON_PARAM, id)
    else p.delete(PERSON_PARAM)
    return p
  }, { replace: true })
  const segBtn = (chosen: boolean) => `${BUTTON_BASE} ${BUTTON_SIZE.sm} ${chosen ? SEGMENT.on : SEGMENT.off}`

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-bold font-display text-primary">Conversations</h1>
        <div role="group" aria-label="View" className="shrink-0 flex items-center gap-1">
          <button
            type="button"
            onClick={() => pickView(VIEW.CONVERSATIONS)}
            aria-pressed={view === VIEW.CONVERSATIONS}
            className={segBtn(view === VIEW.CONVERSATIONS)}
          >
            Conversations
          </button>
          <button
            type="button"
            onClick={() => pickView(VIEW.PEOPLE)}
            aria-pressed={view === VIEW.PEOPLE}
            className={segBtn(view === VIEW.PEOPLE)}
          >
            People
          </button>
        </div>
      </div>

      {view === VIEW.PEOPLE
        ? <PeopleView agent={name} selected={person} onSelect={pickPerson} />
        : <ConversationsView agent={name} />}
    </div>
  )
}
