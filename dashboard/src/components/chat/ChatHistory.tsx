import { useState, useMemo, useEffect, useRef, useCallback, type JSX, useLayoutEffect } from 'react'
import {
  Chat, TaskChat, useDeleteChat, useRenameChat, useRenameTask,
  useSearchChats, useTaskChats,
} from '../../api/chats'
import { useChatSlice } from '../../store/chatStore'
import { useActiveChats } from '../../hooks/useActiveChats'
import { pushEscHandler } from '../../lib/escStack'
import Button from '../ui/Button'
import Tooltip from '../ui/Tooltip'
import { POPOVER } from '../ui/classes'
import { rowAccentClass } from './projectAccents'
import ActiveChatsPanel from './ActiveChatsPanel'
import MoveChatConfirm from './MoveChatConfirm'
import { leavesOffline, MOVE_MEMORY, moveMemoryOf, useChatTargets } from '../../api/remoteMachines'
import SharePopover, { type ShareTarget } from '../sharing/SharePopover'
import { SOURCE_TYPE } from '../../lib/session/kind'
import { isSharedChatOwner } from '../../lib/visibility'
import { CHAT_PHASE, isLiveChatPhase } from '../../lib/status/chat'
import { isLiveRunStatus } from '../../lib/status/run'
import { unreadRowClass } from '../../lib/chat/unreadRow'
import AlertDialog from '../ui/AlertDialog'

// Small status indicator next to a chat row title — warm/fail only. The live
// (generating) and unread states paint the ROW background instead (see
// ChatRow): the pulsing action-subtle tint while generating, the static tint
// for a response this viewer has not opened.
function ChatStatusDot({ chatId }: { chatId: string }) {
  const slice = useChatSlice(chatId)
  const status = slice?.status
  if (status === CHAT_PHASE.WARMING) {
    return (
      <span
        title="Preparing remote environment…"
        className="inline-block w-1.5 h-1.5 rounded-full bg-status-warning-solid animate-pulse motion-reduce:animate-none mr-1.5 shrink-0"
      />
    )
  }
  if (status === CHAT_PHASE.FAILED) {
    return (
      <span
        title="Warmup failed"
        className="inline-block w-1.5 h-1.5 rounded-full bg-status-danger-solid mr-1.5 shrink-0"
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
  /** Fires the move_chat WS op — the op acts on the connection's OPEN chat,
      so the kebab's move action renders only on the active row (other rows
      with mismatch data get a plain "Runs on <target>" info row). */
  onMoveChat?: () => void
  /** True while an inline rename input is open — the page pauses the chat
      list's poll (a poll-driven reorder moves the row's DOM node, which
      blurs the input and silently discards the user's in-progress edit). */
  onRenameEditingChange?: (editing: boolean) => void
}

/** The inline rename editor — swapped in place of a row's title block.
 * Uncontrolled (the row's edit lifecycle is keyed in ChatHistory, not the
 * row, so a row remount can't drop keystrokes). SAVING is explicit — the ✓
 * button or Enter; Esc, clicking/tapping anywhere else, and any blur all
 * CANCEL. The asymmetry is deliberate (operator-hit on mobile 2026-07-25):
 * the select-all prefill puts an accidental wipe one keystroke away, and a
 * committed rename also stamps `title_generated` (auto-titling off for that
 * chat forever) — the destructive outcome must never ride the ambient
 * gesture. The ✓ saves on POINTERDOWN with preventDefault: pointerdown
 * fires before the input's blur on both mouse and touch (blur-then-click
 * would cancel the edit before the save could land) and keeps focus in the
 * input; `doneRef` collapses the follow-up click/blur into a no-op. An
 * unchanged/empty commit is a cancel — an unchanged commit would still
 * stamp `title_generated` server-side. */
function RenameInput({ initial, onCommit, onCancel }: {
  initial: string
  onCommit: (value: string) => void
  onCancel: () => void
}) {
  const ref = useRef<HTMLInputElement>(null)
  const doneRef = useRef(false)
  const cancelRef = useRef(onCancel)
  useLayoutEffect(() => { cancelRef.current = onCancel })
  useEffect(() => {
    ref.current?.focus()
    ref.current?.select()
    // LIFO above the drawer's Escape handler — Esc must cancel the edit,
    // not also collapse the sidebar/drawer (the input's own keydown stops
    // propagation; this stack entry covers focus-lost edge cases).
    return pushEscHandler(() => cancelRef.current())
  }, [])
  const finish = (commit: boolean) => {
    if (doneRef.current) return
    doneRef.current = true
    const v = (ref.current?.value ?? '').trim()
    if (commit && v && v !== initial.trim()) onCommit(v)
    else onCancel()
  }
  return (
    <div className="flex items-center gap-1 my-0.5">
      <input
        ref={ref}
        defaultValue={initial}
        maxLength={160}
        data-testid="rename-input"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          e.stopPropagation()
          if (e.key === 'Enter') finish(true)
          else if (e.key === 'Escape') finish(false)
        }}
        onBlur={() => finish(false)}
        className="min-w-0 flex-1 text-xs font-medium bg-surface-card text-primary
                   rounded-sm border border-action px-1.5 py-1
                   focus:outline-hidden focus:ring-1 focus:ring-focus-ring/40"
      />
      <button
        data-testid="rename-save"
        aria-label="Save name"
        title="Save"
        onPointerDown={(e) => { e.preventDefault(); e.stopPropagation(); finish(true) }}
        onClick={(e) => { e.stopPropagation(); finish(true) }}
        className="shrink-0 w-7 h-7 rounded-control bg-action text-action-fg flex items-center
                   justify-center hover:bg-action-hover transition-colors"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
        </svg>
      </button>
    </div>
  )
}

/** The active inline edit, threaded to the row being renamed. */
interface RowEdit {
  initial: string
  onCommit: (value: string) => void
  onCancel: () => void
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
      <mark className="bg-action-subtle-hover text-inherit rounded-sm px-0.5">{title.slice(idx, idx + q.length)}</mark>
      {title.slice(idx + q.length)}
    </>
  )
}

/** Three-dot menu for chat items with dropdown and delete confirmation. */
// One sidebar row. Split out of the group map so the per-chat store slice
// (live and unread state) can drive the row's styling with a hook.
// Precedence: active (inverted: the solid action fill with action-subtle
// bars, its text on the fill's foreground) > generating (the pulsing
// action-subtle tint) > unread (the static tint, age-faded) > idle (hover
// only). The active row never renders live or unread paint: viewing it is
// reading it, and the composer already shows its streaming state. The
// inversion keeps the open row apart from a generating one, which wears
// the same tint.
// Generating/unread also carry a 1px inset action ring: the surface tints
// alone blend into the sidebar (especially age-faded unread), and the ring
// stays constant while the background pulses/fades so the row keeps a crisp
// edge. A ring, not a border: the left border is the delegation edge.
function ChatRow({ chat, active, title, plainTitle, onClick, onDelete, onMoveChat,
                   onRename, onShare, canDelete, edit }: {
  chat: Chat
  active: boolean
  title: string | JSX.Element
  /** The row's full label as plain text — the hover/long-press tooltip body
      (the `title` prop can be a search-highlight JSX element). */
  plainTitle: string
  onClick: () => void
  onDelete: (id: string) => void
  onMoveChat?: () => void
  onRename?: () => void
  /** Present → a "Share" row (server-flag gated at the call site). */
  onShare?: () => void
  canDelete: boolean
  edit: RowEdit | null
}) {
  const slice = useChatSlice(chat.id)
  const streaming = slice?.status === CHAT_PHASE.STREAMING
  const unread = slice?.unread !== undefined ? slice.unread : chat.unread
  let stateClass = 'text-secondary hover:bg-surface-hover'
  let stateTitle: string | undefined
  if (active) {
    // Mirrored bars (left + right inset shadows) — the one-sided bar read as
    // a stray accent rail; symmetric they frame the selected row. Colors are
    // the INVERSE of the tinted states: the solid action fill, action-subtle
    // bars, and every text on the fill's own foreground (a dimmed white
    // falls under AA there).
    stateClass = 'bg-action text-action-fg shadow-[inset_3px_0_0_0_var(--color-action-subtle),inset_-3px_0_0_0_var(--color-action-subtle)]'
  } else if (streaming) {
    stateClass =
      'oto-row-live motion-reduce:animate-none bg-action-subtle ring-1 ring-inset ring-action/40 text-secondary'
    stateTitle = 'Generating response…'
  } else if (unread) {
    // A live flip (slice.unread) means the response just landed → full tint.
    const tone = slice?.unread ? 'bg-action-subtle' : unreadRowClass(chat.last_response_at)
    stateClass = `${tone} ring-1 ring-inset ring-action/30 text-secondary hover:bg-surface-hover`
    stateTitle = 'New response, not opened yet'
  }
  // Unified live language (matches ActiveChatsPanel): the dot means exactly
  // one thing — "a finished result you haven't opened". Generating rows pulse
  // without a dot; the active row needs neither (viewing IS reading).
  const unreadDot = !active && !streaming && unread
  return (
    <div
      onClick={edit ? undefined : onClick}
      // aria-label, NOT title: the full-title tooltip owns hover — a native
      // OS tooltip on the row would double up next to it.
      aria-label={stateTitle}
      className={`group flex items-center justify-between px-3 py-2 rounded-card text-sm mb-0.5 cursor-pointer transition-colors border-r-2 border-r-transparent ${
        rowAccentClass(chat, { active })
      } ${stateClass}`}
    >
      <div className="min-w-0 flex-1">
        {edit ? (
          <RenameInput initial={edit.initial} onCommit={edit.onCommit} onCancel={edit.onCancel} />
        ) : (
          <>
            <p className="truncate text-xs font-medium flex items-center">
              <ChatStatusDot chatId={chat.id} />
              {unreadDot && (
                <span
                  title="New response, not opened yet"
                  className="inline-block w-1.5 h-1.5 rounded-full bg-action mr-1.5 shrink-0"
                />
              )}
              <Tooltip text={plainTitle} onlyWhenClipped className="truncate">{title}</Tooltip>
            </p>
            <p className={`text-3xs mt-[2px] ${active ? 'text-action-fg' : 'text-tertiary'}`}>{timeAgo(chat.updated_at)}</p>
          </>
        )}
      </div>
      {!edit && (onRename || onShare || canDelete) && (
        <ChatItemMenu
          chatId={chat.id} onDelete={onDelete} onBrand={active} isOpen={active}
          onMoveChat={onMoveChat} onRename={onRename} onShare={onShare} canDelete={canDelete}
          fullTitle={plainTitle}
        />
      )}
    </div>
  )
}

// One TASK-mode row: a task-run chat with its latest run joined. Purple is
// the task LIVE identity (matches the Active-now strip): purple pulse while
// generating, solid purple inversion when active. The left accent rail is
// the SAME role-based rule as chat rows (rowAccentClass): the agent accent
// at half strength for a delegated worker, the full accent on a wider edge
// for an orchestrator — plain scheduled tasks get none. No unread dot/tint (notifications cover completion). The kebab
// renders Rename/Delete from the server-computed can_* flags — the task
// role matrix lives backend-side; a row with neither right shows no kebab.
function TaskRow({ chat, active, title, plainTitle, onClick, onDelete,
                   onRename, canDelete, edit }: {
  chat: TaskChat
  active: boolean
  title: string | JSX.Element
  plainTitle: string
  onClick: () => void
  onDelete: (id: string) => void
  onRename?: () => void
  canDelete: boolean
  edit: RowEdit | null
}) {
  const slice = useChatSlice(chat.id)
  // Between chat_status frames the joined run status seeds the live state
  // (page load / reconnect); a slice that exists wins in both directions.
  const streaming = slice
    ? slice.status === CHAT_PHASE.STREAMING
    : isLiveRunStatus(chat.run_status)
  let stateClass = 'text-secondary hover:bg-surface-hover'
  let stateTitle: string | undefined
  if (active) {
    // Mirrored edge bars painted with the SIDEBAR BACKGROUND (not a lighter
    // purple) so they read as gaps between the border and the solid fill —
    // the same look as the chat row's action-subtle bars.
    stateClass = 'bg-accent-agent-fill text-on-solid shadow-[inset_3px_0_0_0_var(--color-surface-card),inset_-3px_0_0_0_var(--color-surface-card)]'
  } else if (streaming) {
    stateClass =
      'oto-row-live-purple motion-reduce:animate-none bg-accent-agent-bg ring-1 ring-inset ring-accent-agent-border text-secondary'
    stateTitle = 'Task running…'
  }
  return (
    <div
      onClick={edit ? undefined : onClick}
      aria-label={stateTitle}
      className={`group flex items-center justify-between px-3 py-2 rounded-card text-sm mb-0.5 cursor-pointer transition-colors border-r-2 border-r-transparent ${
        rowAccentClass(chat, { active })
      } ${stateClass}`}
    >
      <div className="min-w-0 flex-1">
        {/* Rows are titled by the task's NAME; the chat title (prompt first
            line / LLM upgrade) drops to the subtitle as per-run context, and
            stays the title for runs whose task row is gone (one-time tasks
            hard-delete after firing → task_name is null). Rename follows the
            label ("rename what you see"): name-labeled rows rename the task
            definition, title-labeled rows rename this run's chat. */}
        {edit ? (
          <RenameInput initial={edit.initial} onCommit={edit.onCommit} onCancel={edit.onCancel} />
        ) : (
          <>
            <p className="text-xs font-medium">
              <Tooltip text={plainTitle} onlyWhenClipped className="block truncate">
                {chat.task_name || title}
              </Tooltip>
            </p>
            <p className={`text-3xs mt-[2px] truncate ${active ? 'text-on-solid' : 'text-tertiary'}`}>
              {chat.task_name && chat.title ? `${chat.title} · ` : ''}{timeAgo(chat.updated_at)}
            </p>
          </>
        )}
      </div>
      {!edit && (onRename || canDelete) && (
        <ChatItemMenu
          chatId={chat.id} onDelete={onDelete} onBrand={active}
          onRename={onRename} canDelete={canDelete} taskKind
          fullTitle={plainTitle}
        />
      )}
    </div>
  )
}

// `onBrand`: the row behind the trigger is the open row on the action fill:
// the trigger takes the fill's foreground so the dots stay visible.
// `isOpen`: this row is the OPEN chat — the move_chat op acts on the
// connection's open chat, so only then is the target-mismatch row actionable.
function ChatItemMenu({ chatId, onDelete, onBrand = false, isOpen = false,
                        onMoveChat, onRename, onShare, canDelete = true,
                        taskKind = false, fullTitle }: {
  chatId: string
  onDelete: (id: string) => void
  onBrand?: boolean
  isOpen?: boolean
  onMoveChat?: () => void
  /** Present → a "Rename" row above Delete (server-flag gated at the call site). */
  onRename?: () => void
  /** Present → a "Share" row: the chat as a read-only snapshot (SHARING.md). */
  onShare?: () => void
  /** False hides the Delete row (e.g. an editor on another user's task run). */
  canDelete?: boolean
  /** Task-history wording for the delete confirmation. */
  taskKind?: boolean
  /** Full row label — shown as the menu header on mobile, where there is no
      hover tooltip path to reveal a clipped title (the long-press tooltip
      exists too; the header costs nothing and is always discoverable). */
  fullTitle?: string
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [confirmMove, setConfirmMove] = useState(false)
  // An item that opens a dialog hands the focus to the kebab first and keeps
  // it shown (it hides when the row loses hover and focus), so the dialog
  // gives the focus back to it when it closes; the kebab's own focus clears it.
  const [holding, setHolding] = useState(false)
  const kebabRef = useRef<HTMLButtonElement>(null)
  const handOff = () => {
    kebabRef.current?.focus()
    setHolding(true)
    setMenuOpen(false)
  }
  const menuRef = useRef<HTMLDivElement>(null)
  // Pin-vs-current target mismatch + live status from the per-chat slice
  // (populated by warmup_ready for chats opened this session; cleared by the
  // first mismatch-free warmup after a move). The selector subscribes, so
  // the row appears/disappears reactively while the menu is open.
  const slice = useChatSlice(chatId)
  const mismatch = slice?.targetMismatch ?? null
  const moveBusy = isLiveChatPhase(slice?.status)
  // The OPEN chat's places (the "Runs on" chip's entry, never fetched per
  // row): a place a person chose is no mismatch to offer a move from.
  const { data: targets } = useChatTargets(isOpen ? chatId : null)
  const placeChosen = isOpen && !!targets?.place_chosen
  // The move confirm goes for good when the move stops being possible (a
  // turn started, the mismatch cleared): it never comes back on its own.
  if (confirmMove && (moveBusy || !mismatch || placeChosen)) setConfirmMove(false)

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

  const handleDeleteClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    handOff()
    setConfirmDelete(true)
  }

  const handleConfirmDelete = useCallback(() => {
    setConfirmDelete(false)
    onDelete(chatId)
  }, [chatId, onDelete])

  const handleMoveClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    handOff()
    setConfirmMove(true)
  }

  const handleConfirmMove = useCallback(() => {
    setConfirmMove(false)
    onMoveChat?.()
  }, [onMoveChat])

  return (
    <>
      {/* Width-based reveal: on desktop a zero-width, clipped wrapper gives
          the title the FULL row width until the row is hovered or focused,
          and the button stays in the Tab order (display: none would drop it,
          an opacity reveal would reserve its width). Shown, the wrapper stops
          clipping so the focus ring is whole. Mobile (no hover) keeps it
          always visible; an open dropdown pins the wrapper so it can't
          vanish mid-interaction. */}
      <div
        ref={menuRef}
        data-testid="chat-row-menu"
        className={`relative ${
          menuOpen || holding
            ? ''
            : 'md:w-0 md:overflow-hidden md:group-hover:w-auto md:group-hover:overflow-visible '
              + 'md:group-focus-within:w-auto md:group-focus-within:overflow-visible'
        }`}
      >
        <button
          ref={kebabRef}
          onClick={(e) => { e.stopPropagation(); setMenuOpen(!menuOpen) }}
          onFocus={() => setHolding(false)}
          className={`p-0.5 rounded-sm transition-colors ml-1 ${
            onBrand
              ? 'text-on-solid hover:bg-action-fg/20'
              : 'text-tertiary hover:text-secondary hover:bg-surface-hover'
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
          <div className={`absolute right-0 top-full mt-1 z-30 ${POPOVER} py-1 min-w-[120px] max-w-[240px]`}>
            {/* Mobile full-title header — no hover on touch, so the menu is
                where a clipped title is guaranteed readable. */}
            {fullTitle && (
              <div className="md:hidden px-3 py-1.5 text-xs font-medium text-primary break-words border-b border-subtle mb-1">
                {fullTitle}
              </div>
            )}
            {/* Target-mismatch row — the banner's permanent home after
                dismissal. Actionable only for the OPEN chat (the move op
                acts on the connection's open chat) and only while its place
                was not chosen by a person's move; other rows just state
                the pin so the fact stays visible. */}
            {mismatch && (isOpen && onMoveChat && !placeChosen ? (
              <button
                onClick={handleMoveClick}
                disabled={moveBusy}
                title={moveBusy ? 'Finish or stop the current turn first' : undefined}
                className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-secondary hover:bg-surface-hover transition-colors disabled:opacity-50 disabled:hover:bg-transparent"
              >
                Runs on {mismatch.pinnedLabel}. Move to {mismatch.resolvedLabel}
              </button>
            ) : (
              <div className="px-3 py-1.5 text-xs text-tertiary">
                Runs on {mismatch.pinnedLabel}
              </div>
            ))}
            {onRename && (
              <button
                onClick={(e) => { e.stopPropagation(); setMenuOpen(false); onRename() }}
                className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-secondary hover:bg-surface-hover transition-colors"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                </svg>
                Rename
              </button>
            )}
            {onShare && (
              <button
                onClick={(e) => { e.stopPropagation(); handOff(); onShare() }}
                className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-secondary hover:bg-surface-hover transition-colors"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8.684 13.342C8.886 12.938 9 12.482 9 12c0-.482-.114-.938-.316-1.342m0 2.684a3 3 0 110-2.684m0 2.684l6.632 3.316m-6.632-6l6.632-3.316m0 0a3 3 0 105.367-2.684 3 3 0 00-5.367 2.684zm0 9.316a3 3 0 105.368 2.684 3 3 0 00-5.368-2.684z" />
                </svg>
                Share
              </button>
            )}
            {canDelete && (
              <button
                onClick={handleDeleteClick}
                className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-status-danger-fg hover:bg-status-danger-bg transition-colors"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                </svg>
                Delete
              </button>
            )}
          </div>
        )}
      </div>

      {confirmDelete && (
        <AlertDialog
          title={taskKind ? 'Delete task run' : 'Delete Chat'}
          confirmLabel="Delete"
          tone="danger"
          onConfirm={handleConfirmDelete}
          onCancel={() => setConfirmDelete(false)}
        >
          {taskKind
            ? 'Removes this run from the task history. The scheduled task itself is not deleted. This action cannot be undone.'
            : 'Are you sure you want to delete this chat? This action cannot be undone.'}
        </AlertDialog>
      )}

      {/* Move confirmation (the same one the "Runs on" chip opens). */}
      {confirmMove && mismatch && (
        <MoveChatConfirm
          target={mismatch.resolvedTarget}
          label={mismatch.resolvedLabel}
          memory={targets ? moveMemoryOf(targets, mismatch.resolvedTarget) : MOVE_MEMORY.FILES}
          fromTarget={targets?.current.target ?? mismatch.pinnedTarget}
          fromLabel={targets?.current.label || mismatch.pinnedLabel}
          fromOffline={targets ? leavesOffline(targets) : false}
          onConfirm={handleConfirmMove}
          onCancel={() => setConfirmMove(false)}
        />
      )}
    </>
  )
}

export default function ChatHistory({
  chats, activeChatId, agentName, onSelect, onNew, onNavigate,
  tasksMode = false, onTasksModeChange, onMoveChat, onRenameEditingChange,
}: Props) {
  const deleteChat = useDeleteChat()
  const renameChat = useRenameChat()
  const renameTask = useRenameTask()
  const [searchInput, setSearchInput] = useState('')
  const debouncedQuery = useDebounce(searchInput, 300)
  const inputRef = useRef<HTMLInputElement>(null)

  // The row being inline-renamed. taskId set = the rename targets the task
  // DEFINITION (name-labeled task rows — "rename what you see"); null = the
  // chat/run title. Keyed here (not row-local) so a row remount — e.g.
  // crossing a day-group boundary on refetch — can't drop the edit.
  const [editing, setEditing] = useState<
    { id: string; taskId: string | null; initial: string } | null>(null)
  useEffect(() => {
    onRenameEditingChange?.(editing !== null)
  }, [editing, onRenameEditingChange])
  // The chat being shared (the row menu's Share): the popover takes the
  // chat as its target and makes a snapshot on the server.
  const [shareTarget, setShareTarget] = useState<ShareTarget | null>(null)

  // Search follows the mode: chat search over the history owner's chats,
  // task search over the agent's task-run chats (run-permission gated).
  const { data: searchResults, isFetching: isSearching } =
    useSearchChats(agentName, debouncedQuery, tasksMode ? 'tasks' : 'chats')
  const { data: taskChats } = useTaskChats(agentName, tasksMode, editing !== null)

  // The tasks toggle pulses purple while this agent has a task generating and
  // the toggle is off — the task rows aren't visible to carry the pulse.
  const activeRows = useActiveChats()
  const hasActiveTasks = useMemo(
    () => activeRows.some((r) =>
      r.sourceType === SOURCE_TYPE.TASK && r.agent === agentName && r.phase === CHAT_PHASE.STREAMING),
    [activeRows, agentName],
  )

  // Use search results when searching, otherwise use the mode's list
  const isSearchActive = debouncedQuery.trim().length > 0
  const modeChats: Chat[] = tasksMode ? (taskChats ?? []) : chats
  const displayChats = isSearchActive && searchResults ? searchResults : modeChats
  const groups = useMemo(() => groupChats(displayChats), [displayChats])

  // A failed DELETE/RENAME otherwise vanishes (the confirm popover / inline
  // input already closed and the list never refetches) — surface it inline
  // above the list. Server 403/409 details are user-phrased.
  const [actionError, setActionError] = useState<string | null>(null)
  const actionErrorTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (actionErrorTimer.current) clearTimeout(actionErrorTimer.current)
  }, [])
  const surfaceError = useCallback((message: string) => {
    setActionError(message)
    if (actionErrorTimer.current) clearTimeout(actionErrorTimer.current)
    actionErrorTimer.current = setTimeout(() => setActionError(null), 8000)
  }, [])

  const handleDelete = useCallback((chatId: string) => {
    // The open chat is left BEFORE the request: the server ends its turn
    // and closes its terminal as the row goes, and the frames of that end
    // must meet a view on no chat. A refused delete (the chat still
    // starting, a run still live) puts the person back on it.
    const wasOpen = chatId === activeChatId
    if (wasOpen) onNew()
    deleteChat.mutate(chatId, {
      onError: (e) => {
        surfaceError((e as Error)?.message || 'Failed to delete chat')
        if (wasOpen) onSelect(chatId)
      },
    })
  }, [deleteChat, surfaceError, activeChatId, onNew, onSelect])

  const startRename = useCallback((chat: Chat | TaskChat) => {
    const tc = chat as TaskChat
    const nameLabeled = tasksMode && !!tc.task_name && !!tc.task_id
    setEditing({
      id: chat.id,
      taskId: nameLabeled ? (tc.task_id as string) : null,
      // Prefill from DATA, never the rendered title prop (search mode passes
      // a JSX highlight element) and never the 'New Chat' placeholder.
      initial: nameLabeled ? (tc.task_name as string) : (chat.title || ''),
    })
  }, [tasksMode])

  const cancelRename = useCallback(() => setEditing(null), [])
  const commitRename = useCallback((value: string) => {
    if (!editing) return
    const onError = (e: unknown) =>
      surfaceError((e as Error)?.message || 'Failed to rename')
    if (editing.taskId) {
      renameTask.mutate({ taskId: editing.taskId, name: value }, { onError })
    } else {
      renameChat.mutate({ chatId: editing.id, title: value }, { onError })
    }
    setEditing(null)
  }, [editing, renameChat, renameTask, surfaceError])

  return (
    <div className="w-full border-r border-subtle bg-surface-card flex flex-col h-full">
      {/* Header: New Chat + Search (the mode title row sits below the
          Active-now strip, directly above the list it labels) */}
      <div className="p-3 border-b border-subtle space-y-2">
        <Button
          variant="primary"
          className="w-full"
          onClick={() => {
            // Starting a chat FROM the task view exits it — the new-chat
            // page is a chat surface, so the list below follows.
            if (tasksMode) onTasksModeChange?.(false)
            onNew()
            closeMobileDrawer(onNavigate)
          }}
        >
          + New Chat
        </Button>

        {/* Search input */}
        <div className="relative">
          <svg
            className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-tertiary pointer-events-none"
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
            className="w-full pl-8 pr-7 py-1.5 text-xs rounded-control border border-strong bg-surface-card
                       text-primary placeholder:text-tertiary
                       focus:outline-hidden focus:ring-1 focus:ring-focus-ring/40 focus:border-action
                       transition-colors"
          />
          {/* Clear button */}
          {searchInput && (
            <button
              onClick={() => { setSearchInput(''); inputRef.current?.focus() }}
              className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 rounded-full
                         bg-surface-muted flex items-center justify-center
                         text-tertiary hover:text-secondary transition-colors"
            >
              <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>

        {actionError && (
          <div className="flex items-start gap-2 px-2.5 py-1.5 rounded-control border border-status-danger-border bg-status-danger-bg text-status-danger-fg text-xs">
            <span className="min-w-0 break-words">{actionError}</span>
            <button
              onClick={() => setActionError(null)}
              className="ml-auto shrink-0 hover:text-status-danger-fg"
              aria-label="Dismiss"
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        )}
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
        <p className="text-xs font-semibold text-secondary">
          {tasksMode ? 'Task history' : 'Chat history'}
        </p>
        {onTasksModeChange && (
          <button
            onClick={() => onTasksModeChange(!tasksMode)}
            title={tasksMode ? 'Show chat history' : 'Show task history'}
            data-testid="tasks-toggle"
            className={`w-7 h-7 rounded-control border flex items-center justify-center transition-colors cursor-pointer ${
              tasksMode
                ? 'bg-accent-agent-fill border-accent-agent-fill text-on-solid hover:brightness-110'
                : `bg-accent-agent-bg border-accent-agent-border text-accent-agent-fg hover:brightness-95 ${
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
            <span className="inline-block w-3 h-3 border-2 border-action border-t-transparent rounded-full animate-spin" />
          </div>
        )}

        {/* No results */}
        {isSearchActive && !isSearching && displayChats.length === 0 && (
          <p className="text-xs text-tertiary text-center py-4">No matches found</p>
        )}

        {/* Grouped results — same day grouping in both modes */}
        {groups.map((group) => (
          <div key={group.label} className="mb-3">
            <p className="px-3 py-1 text-3xs font-semibold text-tertiary uppercase tracking-wider">
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
              // Server-computed flags; absent (older-proxy cache) → chats keep
              // their historical owner rights, task rows stay menu-less.
              const canRename = chat.can_rename ?? !tasksMode
              const canDelete = chat.can_delete ?? !tasksMode
              const canShare = !tasksMode && !!chat.can_share
              const edit = editing?.id === chat.id
                ? { initial: editing.initial, onCommit: commitRename, onCancel: cancelRename }
                : null
              return tasksMode ? (
                <TaskRow
                  key={chat.id}
                  chat={chat as TaskChat}
                  active={chat.id === activeChatId}
                  title={title}
                  plainTitle={(chat as TaskChat).task_name || chat.title || 'New Chat'}
                  onClick={open}
                  onDelete={handleDelete}
                  onRename={canRename ? () => startRename(chat) : undefined}
                  canDelete={canDelete}
                  edit={edit}
                />
              ) : (
                <ChatRow
                  key={chat.id}
                  chat={chat}
                  active={chat.id === activeChatId}
                  title={title}
                  plainTitle={chat.title || 'New Chat'}
                  onClick={open}
                  onDelete={handleDelete}
                  onMoveChat={onMoveChat}
                  onRename={canRename ? () => startRename(chat) : undefined}
                  onShare={canShare ? () => setShareTarget({
                    kind: 'chat', id: chat.id, title: chat.title || 'New Chat',
                    scope: isSharedChatOwner(chat.user_sub) ? 'shared' : 'personal',
                  }) : undefined}
                  canDelete={canDelete}
                  edit={edit}
                />
              )
            })}
          </div>
        ))}
        {shareTarget && <SharePopover target={shareTarget} onClose={() => setShareTarget(null)} />}

        {/* Empty state */}
        {!isSearchActive && modeChats.length === 0 && (
          <p className="text-xs text-tertiary text-center py-4">
            {tasksMode ? 'No task runs yet' : 'No chats yet'}
          </p>
        )}
      </div>
    </div>
  )
}
