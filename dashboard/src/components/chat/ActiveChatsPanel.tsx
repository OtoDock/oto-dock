import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAgents } from '../../api/agents'
import { useActiveChats, type ActiveChatRow } from '../../hooks/useActiveChats'
import { useChatStore } from '../../store/chatStore'

// Collapsed row cap; the rest sit behind a "+N more" expander.
const VISIBLE_CAP = 6

interface Props {
  currentAgent?: string
  activeChatId: string | null
  // Same-agent rows select in place (identical to clicking a sidebar row);
  // foreign-agent rows navigate to /chat/:agent/:chatId.
  onSelect: (chatId: string) => void
  onNavigate?: () => void
  /** 'sidebar' (default): the compact section at the top of the chat list.
      'home': the agent front page's live-sessions card — the platform panel
      above the agent's dashboards, mirroring the project dock composition. */
  variant?: 'sidebar' | 'home'
  /** Sidebar only: which list renders BELOW the strip. The own-agent dedup
      follows it — task view keeps own live chats (no chat list below to
      duplicate) and drops own tasks (they render in the task list). */
  tasksMode?: boolean
}

/** Cross-agent "Active now" section — sidebar top slot AND the agent home.
 *
 * Every chat this user may see that is generating (pulse) or warming (amber
 * dot) RIGHT NOW, across all agents — one click from the conversation.
 * Renders nothing when nothing is active. Live via the WS events the client
 * already receives (see useActiveChats); the currently-viewed chat is
 * excluded — viewing it IS watching it (same rule as ChatRow's precedence).
 *
 * The SIDEBAR variant additionally drops the current agent's own rows that
 * the LIST BELOW already shows live-styled — the dedup follows the sidebar
 * mode: chat view drops all own-agent rows (chats sit in the history list;
 * own tasks pulse the tasks toggle instead), task view drops only own-agent
 * TASK rows (they render in the task list) and keeps own live chats.
 * Everything from other agents stays. The HOME variant keeps all rows — the
 * front page has no list to duplicate against.
 */
export default function ActiveChatsPanel({ currentAgent, activeChatId, onSelect, onNavigate, variant = 'sidebar', tasksMode = false }: Props) {
  const navigate = useNavigate()
  const rows = useActiveChats().filter((r) =>
    r.id !== activeChatId &&
    (variant !== 'sidebar' || r.agent !== currentAgent ||
      (tasksMode && r.sourceType !== 'task')))
  const { data: agents } = useAgents()
  const [expanded, setExpanded] = useState(false)

  if (rows.length === 0) return null

  const agentMeta = new Map((agents || []).map((a) => [a.name, a]))
  const visible = expanded ? rows : rows.slice(0, VISIBLE_CAP)
  const hidden = rows.length - visible.length

  const open = (row: ActiveChatRow) => {
    // Clicking IS seeing: retire the finished-unread row immediately (the
    // chat page confirms via chat_read).
    useChatStore.getState().setUnread(row.id, false)
    if (row.sourceType === 'task') {
      // Task runs render on the chat page — open it with task mode on.
      navigate(`/chat/${row.agent}/${row.id}?tasks=1`)
    } else if (row.agent === currentAgent) {
      onSelect(row.id)
    } else {
      navigate(`/chat/${row.agent}/${row.id}`)
    }
    if (window.innerWidth < 768 && onNavigate) onNavigate()
  }

  const body = (
    <>
      <p className="px-3 py-1 text-[10px] font-semibold text-p-text-light uppercase tracking-wider">
        Active now
      </p>
      {visible.map((row) => {
        const meta = agentMeta.get(row.agent)
        const isTask = row.sourceType === 'task'
        // Unified live language (operator ask, 2026-07-11 — same as the chat
        // history rows): GENERATING = pulsing surface tint only, no dot;
        // FINISHED-UNREAD = the same tint held steady + a dot. The dot means
        // exactly one thing everywhere: "a result you haven't opened".
        // Task rows keep their purple identity, chats the brand blue.
        const phaseClass =
          row.phase === 'streaming'
            ? isTask
              ? 'oto-row-live-purple motion-reduce:animate-none bg-p-accent-purple/10 ring-1 ring-inset ring-p-accent-purple/40'
              : 'oto-row-live motion-reduce:animate-none bg-brand-surface ring-1 ring-inset ring-brand/35'
            : row.phase === 'warming'
              ? 'ring-1 ring-inset ring-amber-400/40'
              : isTask
                ? 'bg-p-accent-purple/10 ring-1 ring-inset ring-p-accent-purple/30'
                : 'bg-brand-surface ring-1 ring-inset ring-brand/30'
        const phaseTitle =
          row.phase === 'streaming'
            ? isTask ? 'Task running…' : 'Generating response…'
            : row.phase === 'warming'
              ? 'Preparing session…'
              : isTask ? 'Task finished' : 'Finished — not opened yet'
        // Tasks never carry the unread dot (fire-and-forget — notifications
        // cover completion); the row itself lingers briefly, dot-free.
        const showDot = row.phase === 'warming' || (row.phase === 'finished' && !isTask)
        return (
          <div
            key={row.id}
            onClick={() => open(row)}
            title={phaseTitle}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm mb-0.5 cursor-pointer
                        transition-colors text-p-text-secondary hover:bg-p-surface-hover ${phaseClass}`}
          >
            {showDot && (
              <span
                className={`inline-block w-2 h-2 rounded-full shrink-0 ${
                  row.phase === 'warming'
                    ? 'bg-amber-400 animate-pulse motion-reduce:animate-none'
                    : ''
                }`}
                style={row.phase === 'warming' ? undefined : { backgroundColor: meta?.color || 'var(--color-brand)' }}
              />
            )}
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium">{row.title || 'New chat'}</p>
              <p className="truncate text-[10px] text-p-text-light mt-[1px]">
                {meta?.display_name || row.agent}{isTask ? ' · task' : ''}
              </p>
            </div>
          </div>
        )
      })}
      {hidden > 0 && (
        <button
          onClick={() => setExpanded(true)}
          className="w-full px-3 py-1 text-[10px] text-p-text-light hover:text-p-text-secondary text-left transition-colors"
        >
          +{hidden} more
        </button>
      )}
    </>
  )

  if (variant === 'home') {
    return (
      <div className="px-4 pt-14 shrink-0" data-testid="active-chats-home">
        {/* 5xl matches the column the pinned dashboards center themselves in,
            so the platform panel and the mini app below read as one page. */}
        <div className="max-w-5xl mx-auto mt-2 rounded-xl border border-p-border-light bg-white dark:bg-p-surface px-2 pt-1.5 pb-1">
          {body}
        </div>
      </div>
    )
  }
  return (
    <div className="px-2 pt-2 pb-1 border-b border-p-border-light" data-testid="active-chats-panel">
      {body}
    </div>
  )
}
