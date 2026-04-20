import { useState, useMemo, useEffect, useRef, useCallback, type JSX } from 'react'
import { createPortal } from 'react-dom'
import { Chat, TaskChat, useDeleteChat, useSearchChats, useTaskChats } from '../../api/chats'
import { useChatSlice } from '../../store/chatStore'
import { useActiveChats } from '../../hooks/useActiveChats'
import { rowAccentClass } from './projectAccents'
import ActiveChatsPanel from './ActiveChatsPanel'

// Unread-row age steps: a fresh response tints the whole row with the full
// brand surface; one that has sat unread fades in two steps, so the sidebar
// separates "just finished" from "waiting since yesterday". A live store flip
// has no timestamp and counts as fresh (it IS fresh — the turn just ended).
export function unreadRowClass(lastResponseAt?: string | null): string {
  const ts = lastResponseAt ? Date.parse(lastResponseAt) : NaN
  if (Number.isNaN(ts)) return 'bg-brand-surface'
  const ageHours = (Date.now() - ts) / 3_600_000
  if (ageHours < 6) return 'bg-brand-surface'
  if (ageHours < 24) return 'bg-brand-surface/60'
  return 'bg-brand-surface/35'
}

// Small status indicator next to a chat row title — warm/fail only. The live
// (generating) and unread states paint the ROW background instead (see
// ChatRow): pulsing brand surface while generating, static brand surface for
// a response this viewer hasn't opened.
function ChatStatusDot({ chatId }: { chatId: string }) {
  const slice = useChatSlice(chatId)
  const status = slice?.status
  if (status === 'warming') {
    return (
      <span
        title="Preparing remote environment…"
        className="inline-block w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse motion-reduce:animate-none mr-1.5 shrink-0"
      />
    )
  }
  if (status === 'failed') {
    return (
      <span
        title="Warmup failed"
        className="inline-block w-1.5 h-1.5 rounded-full bg-red-500 mr-1.5 shrink-0"
      />
    )
  }
  return null
}

interface Props {
  chats: Chat[]
  activeChatId: string | null
  agentName?: string
  onSelect: (chatId: string, searchQuery?: string) => void
  onNew: () => void
  onNavigate?: () => void
  /** Task mode: the list shows the agent's task-run chats instead of the chat
      history (controlled by the page — ?tasks=1 deep links toggle it on). */
  tasksMode?: boolean
  onTasksModeChange?: (on: boolean) => void
}

function timeAgo(dateStr: string): string {
  const now = Date.now()
  const then = new Date(dateStr).getTime()
  const diff = now - then
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  return `${days}d`
}

function closeMobileDrawer(onNavigate?: () => void) {
  if (window.innerWidth < 768 && onNavigate) onNavigate()
}

interface ChatGroup {
  label: string
  chats: Chat[]
}

function groupChats(chats: Chat[]): ChatGroup[] {
  const now = new Date()
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const dayOfWeek = now.getDay() || 7
  const weekStart = todayStart - (dayOfWeek - 1) * 86400000

  const today: Chat[] = []
  const thisWeek: Chat[] = []
  const older: Chat[] = []

  for (const chat of chats) {
    const t = new Date(chat.updated_at).getTime()
    if (t >= todayStart) {
      today.push(chat)
    } else if (t >= weekStart) {
      thisWeek.push(chat)
    } else {
      older.push(chat)
    }
  }

  const groups: ChatGroup[] = []
  if (today.length > 0) groups.push({ label: 'Today', chats: today })
  if (thisWeek.length > 0) groups.push({ label: 'This Week', chats: thisWeek })
  if (older.length > 0) groups.push({ label: 'Previous', chats: older })
  return groups
}

// Debounce hook
function useDebounce(value: string, delay: number): string {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}

/** Highlight the first occurrence of `query` in `title` with brand styling. */
function highlightTitle(title: string, query: string): JSX.Element {
  if (!query) return <>{title}</>
  const lower = title.toLowerCase()
  const q = query.toLowerCase()
  const idx = lower.indexOf(q)
  if (idx === -1) return <>{title}</>
  return (
    <>
      {title.slice(0, idx)}
      <mark className="bg-brand-100 text-inherit rounded-sm px-0.5">{title.slice(idx, idx + q.length)}</mark>
      {title.slice(idx + q.length)}
    </>
  )
}

/** Three-dot menu for chat items with dropdown and delete confirmation. */
// One sidebar row. Split out of the group map so the per-chat store slice
// (live/unread state) can drive the ROW styling with a hook. Precedence:
// active (INVERTED: solid brand fill + brand-surface bars, white text) >
// generating (pulsing brand surface) > unread (static brand surface,
// age-faded) > idle (hover only). The active row never renders live/unread
// paint — viewing it IS reading it, and the composer already shows its
// streaming state. The inversion is deliberate (operator ask): active and
// generating both wore the brand-surface tint and read as the same state —
// the solid fill makes the selection unmistakable at a glance.
// Generating/unread also carry a 1px inset brand ring: the surface tints
// alone blend into the sidebar (especially age-faded unread), and the ring
// stays constant while the background pulses/fades so the row keeps a crisp
// edge. Ring, not border — border-l is the project accent rail.
function ChatRow({ chat, active, title, onClick, onDelete }: {
  chat: Chat
  active: boolean
  title: string | JSX.Element
  onClick: () => void
  onDelete: (id: string) => void
}) {
  const slice = useChatSlice(chat.id)
  const streaming = slice?.status === 'streaming'
  const unread = slice?.unread !== undefined ? slice.unread : chat.unread
  let stateClass = 'text-p-text-secondary hover:bg-p-surface-hover'
  let stateTitle: string | undefined
  if (active) {
    // Mirrored bars (left + right inset shadows) — the one-sided bar read as
    // a stray accent rail; symmetric they frame the selected row. Colors are
    // the INVERSE of the tinted states: solid brand fill, brand-surface bars.
    stateClass = 'bg-brand text-white shadow-[inset_3px_0_0_0_var(--color-brand-surface),inset_-3px_0_0_0_var(--color-brand-surface)]'
  } else if (streaming) {
    stateClass =
      'oto-row-live motion-reduce:animate-none bg-brand-surface ring-1 ring-inset ring-brand/35 text-p-text-secondary'
    stateTitle = 'Generating response…'
  } else if (unread) {
    // A live flip (slice.unread) means the response just landed → full tint.
    const tone = slice?.unread ? 'bg-brand-surface' : unreadRowClass(chat.last_response_at)
    stateClass = `${tone} ring-1 ring-inset ring-brand/30 text-p-text-secondary hover:bg-p-surface-hover`
    stateTitle = 'New response — not opened yet'
  }
  // Unified live language (matches ActiveChatsPanel): the dot means exactly
  // one thing — "a finished result you haven't opened". Generating rows pulse
  // without a dot; the active row needs neither (viewing IS reading).
  const unreadDot = !active && !streaming && unread
  return (
    <div
      onClick={onClick}
      title={stateTitle}
      className={`group flex items-center justify-between px-3 py-2 rounded-lg text-sm mb-0.5 cursor-pointer transition-colors border-l-2 border-r-2 border-r-transparent ${
        rowAccentClass(chat, { active }) || 'border-l-transparent'
      } ${stateClass}`}
    >
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs font-medium flex items-center">
          <ChatStatusDot chatId={chat.id} />
          {unreadDot && (
            <span
              title="New response — not opened yet"
              className="inline-block w-1.5 h-1.5 rounded-full bg-brand mr-1.5 shrink-0"
            />
          )}
          <span className="truncate">{title}</span>
        </p>
        <p className={`text-[10px] mt-[2px] ${active ? 'text-white/70' : 'text-p-text-light'}`}>{timeAgo(chat.updated_at)}</p>
      </div>
      <ChatItemMenu chatId={chat.id} onDelete={onDelete} onBrand={active} />
    </div>
  )
}

// One TASK-mode row: a task-run chat with its latest run joined. Purple is
// the task LIVE identity (matches the Active-now strip): purple pulse while
// generating, solid purple inversion when active. The left accent rail is
// the SAME role-based rule as chat rows (rowAccentClass): violet only for
// delegated workers, amber for orchestrators — plain scheduled tasks get
// none. Deliberately NO unread dot/tint and NO delete menu — tasks are
// fire-and-forget (notifications cover completion; the admin History audits).
function TaskRow({ chat, active, title, onClick }: {
  chat: TaskChat
  active: boolean
  title: string | JSX.Element
  onClick: () => void
}) {
  const slice = useChatSlice(chat.id)
  // Between chat_status frames the joined run status seeds the live state
  // (page load / reconnect); a slice that exists wins in both directions.
  const streaming = slice
    ? slice.status === 'streaming'
    : chat.run_status === 'running' || chat.run_status === 'pending'
  let stateClass = 'text-p-text-secondary hover:bg-p-surface-hover'
  let stateTitle: string | undefined
  if (active) {
    // Mirrored edge bars painted with the SIDEBAR BACKGROUND (not a lighter
    // purple) so they read as gaps between the border and the solid fill —
    // the same look as the chat row's brand-surface bars.
    stateClass = 'bg-p-accent-purple text-white shadow-[inset_3px_0_0_0_var(--color-p-bg),inset_-3px_0_0_0_var(--color-p-bg)]'
  } else if (streaming) {
    stateClass =
      'oto-row-live-purple motion-reduce:animate-none bg-p-accent-purple/10 ring-1 ring-inset ring-p-accent-purple/40 text-p-text-secondary'
    stateTitle = 'Task running…'
  }
  return (
    <div
      onClick={onClick}
      title={stateTitle}
      className={`group flex items-center justify-between px-3 py-2 rounded-lg text-sm mb-0.5 cursor-pointer transition-colors border-l-2 border-r-2 border-r-transparent ${
        rowAccentClass(chat, { active }) || 'border-l-transparent'
      } ${stateClass}`}
    >
      <div className="min-w-0 flex-1">
        {/* Rows are titled by the task's NAME; the chat title (prompt first
            line / LLM upgrade) drops to the subtitle as per-run context, and
            stays the title for runs whose task row is gone (one-time tasks
            hard-delete after firing → task_name is null). */}
        <p className="truncate text-xs font-medium">{chat.task_name || title}</p>
        <p className={`text-[10px] mt-[2px] truncate ${active ? 'text-white/70' : 'text-p-text-light'}`}>
          {chat.task_name && chat.title ? `${chat.title} · ` : ''}{timeAgo(chat.updated_at)}
        </p>
      </div>
    </div>
  )
}

// `onBrand`: the row behind the trigger is the solid-brand active row — swap
// the gray trigger colors for white ones so the dots stay visible on blue.
function ChatItemMenu({ chatId, onDelete, onBrand = false }: { chatId: string; onDelete: (id: string) => void; onBrand?: boolean }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpen) return
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [menuOpen])

  const handleDeleteClick = useCallback((e: React.MouseEvent) => {
    e.stopPropagation()
    setMenuOpen(false)
    setConfirmDelete(true)
  }, [])

  const handleConfirmDelete = useCallback((e: React.MouseEvent) => {
    e.stopPropagation()
    setConfirmDelete(false)
    onDelete(chatId)
  }, [chatId, onDelete])

  return (
    <>
      {/* Display-based reveal (not opacity): a hidden wrapper takes no flex
          slot, so the title gets the FULL row width on desktop until hover —
          the old opacity reveal reserved the button's width even while
          invisible. Mobile (no hover) keeps it always visible; an open
          dropdown pins the wrapper so it can't vanish mid-interaction. */}
      <div
        ref={menuRef}
        className={`relative ${
          menuOpen
            ? ''
            : 'hidden group-hover:block group-focus-within:block max-md:block'
        }`}
      >
        <button
          onClick={(e) => { e.stopPropagation(); setMenuOpen(!menuOpen) }}
          className={`p-0.5 rounded-sm transition-colors ml-1 ${
            onBrand
              ? 'text-white/70 hover:text-white hover:bg-white/20'
              : 'text-p-text-light hover:text-p-text-secondary hover:bg-p-surface'
          }`}
          title="Options"
        >
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor">
            <circle cx="12" cy="5" r="1.5" />
            <circle cx="12" cy="12" r="1.5" />
            <circle cx="12" cy="19" r="1.5" />
          </svg>
        </button>

        {/* Dropdown menu */}
        {menuOpen && (
          <div className="absolute right-0 top-full mt-1 z-30 bg-white dark:bg-p-surface rounded-lg border border-p-border-light shadow-lg py-1 min-w-[120px]">
            <button
              onClick={handleDeleteClick}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-p-accent-red hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
              Delete
            </button>
          </div>
        )}
      </div>

      {/* Delete confirmation popup — portal to body so it escapes the drawer's stacking context on mobile */}
      {confirmDelete && createPortal(
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs"
          onClick={(e) => { e.stopPropagation(); setConfirmDelete(false) }}
        >
          <div
            className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light shadow-xl p-6 max-w-sm mx-4"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-semibold text-p-text mb-2">Delete Chat</h3>
            <p className="text-sm text-p-text-secondary mb-5">
              Are you sure you want to delete this chat? This action cannot be undone.
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={(e) => { e.stopPropagation(); setConfirmDelete(false) }}
                className="px-4 py-2 rounded-lg text-sm font-medium text-p-text-secondary
                           bg-p-surface hover:bg-p-surface-hover transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleConfirmDelete}
                className="px-4 py-2 rounded-lg text-sm font-medium text-white
                           bg-p-accent-red hover:bg-red-700 transition-colors"
              >
                Delete
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  )
}

export default function ChatHistory({
  chats, activeChatId, agentName, onSelect, onNew, onNavigate,
  tasksMode = false, onTasksModeChange,
}: Props) {
  const deleteChat = useDeleteChat()
  const [searchInput, setSearchInput] = useState('')
  const debouncedQuery = useDebounce(searchInput, 300)
  const inputRef = useRef<HTMLInputElement>(null)

  // Search follows the mode: chat search over the history owner's chats,
  // task search over the agent's task-run chats (run-permission gated).
  const { data: searchResults, isFetching: isSearching } =
    useSearchChats(agentName, debouncedQuery, tasksMode ? 'tasks' : 'chats')
  const { data: taskChats } = useTaskChats(agentName, tasksMode)

  // The tasks toggle pulses purple while this agent has a task generating and
  // the toggle is off — the task rows aren't visible to carry the pulse.
  const activeRows = useActiveChats()
  const hasActiveTasks = useMemo(
    () => activeRows.some((r) =>
      r.sourceType === 'task' && r.agent === agentName && r.phase === 'streaming'),
    [activeRows, agentName],
  )

  // Use search results when searching, otherwise use the mode's list
  const isSearchActive = debouncedQuery.trim().length > 0
  const modeChats: Chat[] = tasksMode ? (taskChats ?? []) : chats
  const displayChats = isSearchActive && searchResults ? searchResults : modeChats
  const groups = useMemo(() => groupChats(displayChats), [displayChats])

  const handleDelete = useCallback((chatId: string) => {
    deleteChat.mutate(chatId)
  }, [deleteChat])

  return (
    <div className="w-full border-r border-p-border-light bg-p-bg flex flex-col h-full">
      {/* Header: New Chat + Search (the mode title row sits below the
          Active-now strip, directly above the list it labels) */}
      <div className="p-3 border-b border-p-border-light space-y-2">
        <button
          onClick={() => {
            // Starting a chat FROM the task view exits it — the new-chat
            // page is a chat surface, so the list below follows.
            if (tasksMode) onTasksModeChange?.(false)
            onNew()
            closeMobileDrawer(onNavigate)
          }}
          className="w-full px-3 py-1.5 rounded-lg text-sm font-medium text-white
                     bg-brand hover:bg-brand-hover transition-colors"
        >
          + New Chat
        </button>

        {/* Search input */}
        <div className="relative">
          <svg
            className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-p-text-light pointer-events-none"
            fill="none" stroke="currentColor" viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            ref={inputRef}
            type="text"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder={tasksMode ? 'Search tasks...' : 'Search chats...'}
            className="w-full pl-8 pr-7 py-1.5 text-xs rounded-lg border border-p-border-light bg-white dark:bg-p-surface
                       text-p-text placeholder:text-p-text-light
                       focus:outline-hidden focus:ring-1 focus:ring-brand/40 focus:border-brand/40
                       transition-colors"
          />
          {/* Clear button */}
          {searchInput && (
            <button
              onClick={() => { setSearchInput(''); inputRef.current?.focus() }}
              className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 rounded-full
                         bg-p-surface hover:bg-p-border flex items-center justify-center
                         text-p-text-light hover:text-p-text-secondary transition-colors"
            >
              <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Cross-agent "Active now" widget — hidden when nothing is running.
          Its own-agent dedup follows the mode (tasksMode). */}
      <ActiveChatsPanel
        currentAgent={agentName}
        activeChatId={activeChatId}
        onSelect={(cid) => { onSelect(cid); closeMobileDrawer(onNavigate) }}
        onNavigate={onNavigate}
        tasksMode={tasksMode}
      />

      {/* Mode title row — labels the list below it; the toggle mirrors the
          status bar's icon-pill buttons (subtle purple pill off, solid on,
          pulsing while the agent has active tasks and the task view is off). */}
      <div className="flex items-center justify-between px-3 pt-2 pb-1">
        <p className="text-xs font-semibold text-p-text-secondary">
          {tasksMode ? 'Task history' : 'Chat history'}
        </p>
        {onTasksModeChange && (
          <button
            onClick={() => onTasksModeChange(!tasksMode)}
            title={tasksMode ? 'Show chat history' : 'Show task history'}
            data-testid="tasks-toggle"
            className={`w-7 h-7 rounded-lg border flex items-center justify-center transition-colors cursor-pointer ${
              tasksMode
                ? 'bg-p-accent-purple border-p-accent-purple text-white hover:brightness-110'
                : `bg-[#673a97]/10 border-[#673a97]/30 text-p-accent-purple hover:brightness-95 ${
                    hasActiveTasks ? 'animate-pulse motion-reduce:animate-none' : ''
                  }`
            }`}
          >
            {/* Clipboard/task icon (same glyph as the TaskMetadata popup) */}
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor">
              <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2" strokeWidth={1.7} strokeLinecap="round" />
              <rect x="9" y="3" width="6" height="4" rx="1" strokeWidth={1.7} />
              <path d="M9 12h6M9 16h4" strokeWidth={1.5} strokeLinecap="round" />
            </svg>
          </button>
        )}
      </div>

      {/* Chat list */}
      <div className="flex-1 overflow-y-auto p-2">
        {/* Search loading indicator */}
        {isSearchActive && isSearching && (
          <div className="flex items-center justify-center py-3">
            <span className="inline-block w-3 h-3 border-2 border-brand border-t-transparent rounded-full animate-spin" />
          </div>
        )}

        {/* No results */}
        {isSearchActive && !isSearching && displayChats.length === 0 && (
          <p className="text-xs text-p-text-light text-center py-4">No matches found</p>
        )}

        {/* Grouped results — same day grouping in both modes */}
        {groups.map((group) => (
          <div key={group.label} className="mb-3">
            <p className="px-3 py-1 text-[10px] font-semibold text-p-text-light uppercase tracking-wider">
              {group.label}
            </p>
            {group.chats.map((chat) => {
              const title = isSearchActive
                ? highlightTitle(chat.title || 'New Chat', debouncedQuery)
                : (chat.title || 'New Chat')
              const open = () => {
                onSelect(chat.id, isSearchActive ? debouncedQuery : undefined)
                closeMobileDrawer(onNavigate)
              }
              return tasksMode ? (
                <TaskRow
                  key={chat.id}
                  chat={chat as TaskChat}
                  active={chat.id === activeChatId}
                  title={title}
                  onClick={open}
                />
              ) : (
                <ChatRow
                  key={chat.id}
                  chat={chat}
                  active={chat.id === activeChatId}
                  title={title}
                  onClick={open}
                  onDelete={handleDelete}
                />
              )
            })}
          </div>
        ))}

        {/* Empty state */}
        {!isSearchActive && modeChats.length === 0 && (
          <p className="text-xs text-p-text-light text-center py-4">
            {tasksMode ? 'No task runs yet' : 'No chats yet'}
          </p>
        )}
      </div>
    </div>
  )
}
