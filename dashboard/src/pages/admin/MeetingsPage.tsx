import { useState, useMemo } from 'react'
import { useMeetings, Meeting } from '../../api/meetings'
import { useAgents } from '../../api/agents'
import { useAdminUsers } from '../../api/runs'
import StatusBadge from '../../components/StatusBadge'
import { formatRelativeTime } from '../../lib/format'
import MarkdownContent from '../../components/chat/MarkdownContent'
import { MEETING_STATUS } from '../../lib/status/meeting'
import { Select } from '../../components/ui/Field'
import Button from '../../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, CARD } from '../../components/ui/classes'
import { PAGE_TITLE } from '../../components/ui/classes'

// The filter's options: every stored meeting status (the route accepts any).
const STATUSES = ['', ...Object.values(MEETING_STATUS)]

export default function MeetingsPage() {
  const [agentFilter, setAgentFilter] = useState('')
  const [status, setStatus] = useState('')
  const [userFilter, setUserFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const limit = 50

  const { data, isLoading } = useMeetings({
    agent: agentFilter || undefined,
    status: status || undefined,
    created_by: userFilter || undefined,
    limit,
    offset,
  })

  const { data: agents } = useAgents({ all: true })
  const { data: users } = useAdminUsers()
  const agentMap = useMemo(() => {
    const map: Record<string, { display_name: string; color: string }> = {}
    for (const a of agents || []) {
      map[a.name] = { display_name: a.display_name || a.name, color: a.color || '' }
    }
    return map
  }, [agents])

  const userMap = useMemo(() => {
    const map: Record<string, string> = {}
    for (const u of users || []) {
      map[u.sub] = u.name || u.email || u.sub
    }
    return map
  }, [users])

  const total = data?.total ?? 0
  const meetings = data?.meetings ?? []

  const parseParticipants = (m: Meeting): string[] => {
    try { return JSON.parse(m.participants) } catch { return [] }
  }

  return (
    <div className="space-y-4">
      <h1 className={PAGE_TITLE}>Meetings</h1>

      {/* Filters */}
      <div className="flex gap-3 items-center flex-wrap">
        <Select
          value={agentFilter}
          onChange={(e) => { setAgentFilter(e.target.value); setOffset(0) }}
          className="sm:w-auto"
        >
          <option value="">All agents</option>
          {(agents || []).map((a) => (
            <option key={a.name} value={a.name}>{a.display_name || a.name}</option>
          ))}
        </Select>
        <Select
          value={status}
          onChange={(e) => { setStatus(e.target.value); setOffset(0) }}
          className="sm:w-auto"
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s || 'All statuses'}</option>
          ))}
        </Select>
        <Select
          value={userFilter}
          onChange={(e) => { setUserFilter(e.target.value); setOffset(0) }}
          className="sm:w-auto"
        >
          <option value="">All users</option>
          {(users ?? []).map((u) => (
            <option key={u.sub} value={u.sub}>{u.name}</option>
          ))}
        </Select>
        <span className="text-xs text-secondary ml-auto">{total} total</span>
      </div>

      {/* Meeting cards */}
      <div className="space-y-3">
        {isLoading && <p className="text-sm text-secondary py-4">Loading...</p>}
        {!isLoading && meetings.length === 0 && <p className="text-sm text-secondary py-4">No meetings found.</p>}
        {meetings.map((m) => {
          const participants = parseParticipants(m)
          const expanded = expandedId === m.id
          const moderatorInfo = agentMap[m.moderator]
          const creatorName = m.created_by ? (userMap[m.created_by] || m.created_by) : null
          return (
            <div
              key={m.id}
              className={`${CARD} overflow-hidden`}
            >
              {/* Collapsed header */}
              <button
                onClick={() => setExpandedId(expanded ? null : m.id)}
                className="w-full text-left p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-primary truncate">{m.topic}</p>
                    <div className="flex flex-wrap gap-x-4 gap-y-1 mt-1.5 text-xs text-secondary">
                      <span>{m.created_at ? formatRelativeTime(m.created_at) : '\u2014'}</span>
                      {moderatorInfo && (
                        <span className="inline-flex items-center gap-1">
                          {moderatorInfo.color && <span className="w-2 h-2 rounded-full" style={{ backgroundColor: moderatorInfo.color }} />}
                          {moderatorInfo.display_name}
                        </span>
                      )}
                      {m.cost_usd > 0 && <span>${m.cost_usd.toFixed(4)}</span>}
                      <span className="inline-flex items-center gap-1">
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" /></svg>
                        {participants.length}
                      </span>
                      <span className={`${BADGE_BASE} ${
                        m.scope === 'agent' ? BADGE_TONE.agent : BADGE_TONE.info
                      }`}>
                        {m.scope}
                      </span>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <StatusBadge status={m.status} />
                    <svg className={`w-4 h-4 text-tertiary transition-transform ${expanded ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </div>
                </div>
              </button>

              {/* Expanded detail */}
              {expanded && (
                <div className="border-t border-subtle px-4 pb-4 space-y-3">
                  {/* Created by */}
                  {creatorName && (
                    <div className="pt-3 text-xs text-secondary">
                      Created by <span className="font-medium text-primary">{creatorName}</span>
                    </div>
                  )}

                  {/* Participants */}
                  <div className={creatorName ? '' : 'pt-3'}>
                    <p className="text-xs font-medium text-secondary uppercase tracking-wide mb-2">Participants</p>
                    <div className="flex flex-wrap gap-2">
                      {participants.map((slug) => {
                        const agent = agentMap[slug]
                        const isMod = slug === m.moderator
                        return (
                          <span
                            key={slug}
                            className="inline-flex items-center gap-1.5 px-2 py-1 rounded-control text-xs bg-surface-muted"
                          >
                            {agent?.color && (
                              <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: agent.color }} />
                            )}
                            <span className="font-medium text-primary">{agent?.display_name || slug}</span>
                            {isMod && <span className="text-3xs text-tertiary">Moderator</span>}
                          </span>
                        )
                      })}
                    </div>
                  </div>

                  {/* Summary */}
                  {m.summary && (
                    <div>
                      <p className="text-xs font-medium text-secondary uppercase tracking-wide mb-2">Summary</p>
                      <div className="text-sm text-primary bg-surface-sunken rounded-card p-3 max-h-64 overflow-y-auto">
                        <MarkdownContent>{m.summary}</MarkdownContent>
                      </div>
                    </div>
                  )}

                  {/* Metadata */}
                  <div className="flex items-center justify-between pt-1">
                    <span className="text-xs text-tertiary font-mono">{m.id}</span>
                    {m.concluded_at && (
                      <span className="text-xs text-secondary">
                        Concluded {formatRelativeTime(m.concluded_at)}
                      </span>
                    )}
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Pagination */}
      {total > limit && (
        <div className="flex items-center gap-3 text-sm">
          <Button
            variant="secondary"
            size="xs"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - limit))}
          >
            Previous
          </Button>
          <span className="text-secondary">
            {offset + 1}&ndash;{Math.min(offset + limit, total)} of {total}
          </span>
          <Button
            variant="secondary"
            size="xs"
            disabled={offset + limit >= total}
            onClick={() => setOffset(offset + limit)}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  )
}
