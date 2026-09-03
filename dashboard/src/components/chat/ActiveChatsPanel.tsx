import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAgents } from '../../api/agents'
import { useActiveChats, type ActiveChatRow } from '../../hooks/useActiveChats'
import { useCollapsePref } from '../../hooks/useCollapsePref'
import { useChatStore } from '../../store/chatStore'

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
      is symmetric to it — chat view drops own chats and keeps own live
      tasks; task view drops own tasks and keeps own live chats. */
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
 * The SIDEBAR variant additionally suppresses SYMMETRICALLY: hide only what
 * the LIST BELOW already shows live-styled. Chat view lists chats → own-agent
 * CHAT rows drop, own live TASKS stay in the strip (the task list isn't on
 * screen; the pulsing tasks toggle alone doesn't say WHICH task runs). Task
 * view lists tasks → own-agent TASK rows drop, own live chats stay.
 * Everything from other agents stays. The HOME variant keeps all rows — the
 * front page has no list to duplicate against.
 *
 * One orphan exemption: chat view, own-agent CHAT rows whose owner is shared
 * (`ownerIsShared` — legacy `agent::` rows a visibility flip left behind).
 * Unless the agent is still shared-only, the chat list below filters those
 * chats OUT, so "the list already shows it" is false and the row stays. Task
 * rows are never orphaned (the task list filters on task_runs scope, not chat
 * owner), so task mode keeps the plain symmetric rule.
 *
 * Space discipline (2026-08-15): the header is a persisted collapse toggle
 * (useCollapsePref — home starts COLLAPSED so the front page belongs to the
 * dashboards, sidebar starts OPEN; a pulsing dot on the collapsed header
 * still signals a generating session) and the rows sit in a height-capped
 * inner-scroll container (~3 rows; the home variant goes two-up on md+ and
 * caps at ~2 grid rows). This replaced the old 6-row "+N more" expander.
 */
export default function ActiveChatsPanel({ currentAgent, activeChatId, onSelect, onNavigate, variant = 'sidebar', tasksMode = false }: Props) {
  const navigate = useNavigate()
  const { data: agents } = useAgents()
  const agentMeta = useMemo(() => new Map((agents || []).map((a) => [a.name, a])), [agents])
  const keepOrphan = (r: ActiveChatRow) => {
    if (tasksMode || r.sourceType === 'task' || !r.ownerIsShared) return false
    const a = agentMeta.get(r.agent)
    // Meta missing OR fields missing → suppress. The widest-mode soft-fall
    // of lib/visibility's modeOfAgent is the WRONG default here (it would
    // re-introduce the duplicate row for shared-only agents while fields
    // are absent), hence the explicit presence checks.
    if (!a || a.collaborative === undefined || a.default_scope === undefined) return false
    return !(a.collaborative === false && a.default_scope === 'agent')
  }
  const rows = useActiveChats().filter((r) =>
    r.id !== activeChatId &&
    (variant !== 'sidebar' || r.agent !== currentAgent ||
      (tasksMode ? r.sourceType !== 'task' : r.sourceType === 'task') ||
      keepOrphan(r)))
  // Persisted per surface and SHARED across mounted copies (the sidebar
  // renders twice inside ResponsiveDrawer). Home starts collapsed — the
  // front page is the dashboards' space; the sidebar starts open.
  const [open, toggleOpen] = useCollapsePref(
    variant === 'home' ? 'active-now-home' : 'active-now-sidebar',
    variant !== 'home',
  )

  if (rows.length === 0) return null

  const anyLive = rows.some((r) => r.phase === 'streaming')

  const openRow = (row: ActiveChatRow) => {
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
      {/* Collapsible header — same typography as the old static label. The
          collapsed home strip still signals liveness via the pulsing dot. */}
      <button
        type="button"
        onClick={toggleOpen}
        aria-expanded={open}
        className="w-full flex items-center gap-1.5 px-3 py-1 text-[10px] font-semibold text-p-text-light uppercase tracking-wider text-left cursor-pointer select-none transition-colors hover:text-p-text-secondary"
      >
        <svg
          className={`w-3 h-3 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
        Active now · {rows.length}
        {!open && anyLive && (
          <span className="w-1.5 h-1.5 rounded-full bg-brand animate-pulse motion-reduce:animate-none shrink-0" />
        )}
      </button>
      {open && (
      <div
        className={
          // Height-capped with inner scroll (~3 single-col rows; the home
          // strip on desktop goes 2-up and caps at ~2 grid rows = 4 cards).
          variant === 'home'
            ? 'grid grid-cols-1 md:grid-cols-2 gap-x-2 max-h-34 md:max-h-23 overflow-y-auto'
            : 'max-h-34 overflow-y-auto'
        }
      >
      {rows.map((row) => {
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
            onClick={() => openRow(row)}
            title={phaseTitle}
            className={`min-w-0 flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm mb-0.5 cursor-pointer
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
      </div>
      )}
    </>
  )

  if (variant === 'home') {
    return (
      <div className="px-2 pt-14 shrink-0" data-testid="active-chats-home">
        {/* Flat px-2 matches the AppsOverlay frame inset (p-2) below, so the
            platform panel and the full-width mini app read as one page. */}
        <div className="mt-2 rounded-xl border border-p-border-light bg-white dark:bg-p-surface px-2 pt-1.5 pb-1">
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
