import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useAgentConversations } from '../../api/chats'
import { formatRelativeTime } from '../../lib/format'

// Dashboard chats are excluded server-side — this tab lists external sessions
// (phone today, webhook/other in future). No "Chat" filter option.
const SOURCE_OPTIONS = [
  { value: '', label: 'All Sources' },
  { value: 'phone', label: 'Phone' },
]

function SourceBadge({ source }: { source?: string }) {
  switch (source) {
    case 'phone':
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-xs bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-300">
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z" /></svg>
          Phone
        </span>
      )
    case 'task':
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-xs bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300">
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
          Task
        </span>
      )
    default:
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-xs bg-brand-100 text-brand dark:bg-brand/10 dark:text-blue-300">
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
        <h1 className="text-xl font-bold text-p-text">Conversations</h1>
        <div className="flex items-center gap-3">
          <select
            value={sourceType}
            onChange={e => { setSourceType(e.target.value); setOffset(0) }}
            className="text-sm border border-p-border-light rounded-lg px-3 py-1.5 bg-white dark:bg-p-surface text-p-text"
          >
            {SOURCE_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <span className="text-sm text-p-text-secondary">{total} total</span>
        </div>
      </div>

      {isLoading && (
        <div className="text-center py-12 text-p-text-light">Loading...</div>
      )}

      {!isLoading && conversations.length === 0 && (
        <div className="text-center py-12 text-p-text-light">
          No phone or external conversations yet. Dashboard chats appear in chat history.
        </div>
      )}

      {conversations.length > 0 && (
        <>
          {/* Desktop table */}
          <div className="hidden md:block bg-white dark:bg-p-surface rounded-xl border border-p-border-light overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-p-bg text-left text-xs text-p-text-secondary uppercase tracking-wide border-b border-p-border-light">
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
                    className="border-b border-p-border-light last:border-0 hover:bg-p-surface-hover cursor-pointer transition-colors"
                  >
                    <td className="px-4 py-3 text-p-text">
                      {c.title || (c.source_type === 'phone' ? 'Phone Call' : 'New Chat')}
                    </td>
                    <td className="px-4 py-3">
                      <SourceBadge source={c.source_type} />
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-p-text-secondary">
                      {(c.total_cost ?? 0) > 0 ? `$${(c.total_cost ?? 0).toFixed(4)}` : '\u2014'}
                    </td>
                    <td className="px-4 py-3 text-p-text-secondary">
                      {formatRelativeTime(c.created_at)}
                    </td>
                    <td className="px-4 py-3 text-p-text-secondary">
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
                className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4 cursor-pointer hover:shadow-xs transition-shadow"
              >
                <div className="flex items-start justify-between mb-2">
                  <p className="text-sm font-medium text-p-text line-clamp-1">
                    {c.title || (c.source_type === 'phone' ? 'Phone Call' : 'New Chat')}
                  </p>
                  <SourceBadge source={c.source_type} />
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-p-text-secondary">
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
            <div className="flex items-center justify-center gap-3 text-sm text-p-text-secondary">
              <button
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - limit))}
                className="px-3 py-1 rounded-lg border border-p-border-light hover:bg-p-surface-hover disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Previous
              </button>
              <span>
                {offset + 1}&ndash;{Math.min(offset + limit, total)} of {total}
              </span>
              <button
                disabled={offset + limit >= total}
                onClick={() => setOffset(offset + limit)}
                className="px-3 py-1 rounded-lg border border-p-border-light hover:bg-p-surface-hover disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
