import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useAgentConversations } from '../../api/chats'
import { formatRelativeTime } from '../../lib/format'
import { SOURCE_TYPE } from '../../lib/session/kind'
import Button from '../../components/ui/Button'
import { Select } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE, CARD } from '../../components/ui/classes'

// Dashboard chats are excluded server-side — this tab lists external sessions
// (phone today, webhook/other in future). No "Chat" filter option.
const SOURCE_OPTIONS = [
  { value: '', label: 'All Sources' },
  { value: SOURCE_TYPE.PHONE, label: 'Phone' },
]

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

export default function AgentConversations() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const [sourceType, setSourceType] = useState('')
  const [offset, setOffset] = useState(0)
  const limit = 25

  const { data, isLoading } = useAgentConversations(name || '', sourceType, offset, limit)
  const conversations = data?.conversations ?? []
  const total = data?.total ?? 0

  if (!name) return null

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-bold font-display text-primary">Conversations</h1>
        <div className="flex items-center gap-3">
          <Select
            value={sourceType} className="max-w-max"
            onChange={e => { setSourceType(e.target.value); setOffset(0) }}
          >
            {SOURCE_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </Select>
          <span className="text-sm text-secondary">{total} total</span>
        </div>
      </div>

      {isLoading && (
        <div className="text-center py-12 text-tertiary">Loading...</div>
      )}

      {!isLoading && conversations.length === 0 && (
        <div className="text-center py-12 text-tertiary">
          No phone or external conversations yet. Dashboard chats appear in chat history.
        </div>
      )}

      {conversations.length > 0 && (
        <>
          {/* Desktop table */}
          <div className={`hidden md:block ${CARD} overflow-hidden`}>
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-surface-sunken text-left text-xs text-secondary uppercase tracking-wide border-b border-subtle">
                  <th className="px-4 py-3">Title</th>
                  <th className="px-4 py-3">Source</th>
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
                    <td className="px-4 py-3 text-primary">
                      {c.title || (c.source_type === SOURCE_TYPE.PHONE ? 'Phone Call' : 'New Chat')}
                    </td>
                    <td className="px-4 py-3">
                      <SourceBadge source={c.source_type} />
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-secondary">
                      {(c.total_cost ?? 0) > 0 ? `$${(c.total_cost ?? 0).toFixed(4)}` : '\u2014'}
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
                  <p className="text-sm font-medium text-primary line-clamp-1">
                    {c.title || (c.source_type === SOURCE_TYPE.PHONE ? 'Phone Call' : 'New Chat')}
                  </p>
                  <SourceBadge source={c.source_type} />
                </div>
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
          {total > limit && (
            <div className="flex items-center justify-center gap-3 text-sm text-secondary">
              <Button
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - limit))}
              >
                Previous
              </Button>
              <span>
                {offset + 1}&ndash;{Math.min(offset + limit, total)} of {total}
              </span>
              <Button
                disabled={offset + limit >= total}
                onClick={() => setOffset(offset + limit)}
              >
                Next
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
