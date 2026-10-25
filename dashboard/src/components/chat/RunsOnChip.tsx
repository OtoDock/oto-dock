/**
 * RunsOnChip — the chat's "Runs on" control in the status bar (ChatStatusBar's
 * `targetSlot`, built by AgentChat): an icon pill of the mode and model
 * pills' shape, a computer glyph only; the tooltip says where the chat runs
 * ("Runs on OtoDock platform", or the machine's name). A click opens the
 * places the server offers (GET /v1/chats/{id}/targets, `useChatTargets`),
 * the current one checked; a pick asks MoveChatConfirm and posts the move
 * (`useMoveChat`, `from` = the place the pill showed). While a move runs the
 * glyph gives way to a ring of the copy's progress (`chat_move_progress`; a
 * spinner while its size is unknown or the target commits) and the pill
 * takes no click; the `chat_moved` frame refreshes the answer. A new chat
 * opens the same menu over where this person's new chats may start: a pick
 * needs no confirm (nothing moves yet) and the page sends it with the
 * chat's first warmup. A chat that cannot move (a phone chat, a Direct LLM
 * chat, a person who cannot drive it) or that runs on someone's computer
 * shows its place with the server's sentence in the tooltip.
 *
 * `ChatTargetsMenu` is the menu button itself (the pill's and the
 * ChatTargetBanner's), `ChatLockNotice` the line the page shows while the
 * chat is read-only for this person.
 */
import { useEffect, useState, type CSSProperties, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'

import {
  leavesOffline, moveMemoryOf, useAgentRunTargets, useChatTargets, useMoveChat,
  type ChatTargets, type MoveProgress, type RunTarget,
} from '../../api/remoteMachines'
import { MOVE_PHASE } from '../../api/wireEvents'
import { usePortalMenu } from '../../hooks/usePortalMenu'
import { toastError } from '../../lib/http/apiJson'
import { isLocalTarget } from '../../lib/placement'
import { MACHINE_STATE } from '../../lib/status/machine'
import { dotColorClass } from '../remoteBadgeDot'
import { POPOVER } from '../ui/classes'
import MoveChatConfirm from './MoveChatConfirm'

interface Props {
  /** The open chat; null on a new chat (the pill picks where it starts). */
  chatId: string | null
  agentName: string
  /** Non-empty while a move cannot start (a turn runs or the chat warms):
   *  the pill takes no click and says this. */
  disabledReason: string
  /** After the server accepted a move (202). */
  onMoved?: () => void
  /** A new chat: the engine the page would start it on (the places that
   *  can run it), the place picked so far ('' = the default) and the pick. */
  engine?: string
  pickedPlace?: string
  onPickPlace?: (target: string, isDefault: boolean) => void
}

/** The look of the status bar's icon pills (the mode and model buttons'). */
const PILL = 'flex items-center justify-center w-7 h-7 shrink-0 rounded-control border '
  + 'bg-surface-muted border-subtle text-secondary'
const PILL_BUTTON = 'hover:bg-surface-card hover:border-default transition-colors '
  + 'cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-surface-muted'
// A pill whose menu cannot open now still opens its note (a touch screen
// shows no tooltip), so it looks unavailable without being disabled.
const PILL_RESTING = 'aria-disabled:opacity-60 aria-disabled:hover:bg-surface-muted'

/** A computer: where the chat runs. */
function ComputerGlyph() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
    </svg>
  )
}

function Spinner() {
  return (
    <span
      className="inline-block w-3.5 h-3.5 shrink-0 border-2 border-action border-t-transparent rounded-full animate-spin"
      data-testid="move-spinner"
      aria-hidden="true"
    />
  )
}

/** A move's copy, 0 to 100, in the brand colour; a spinner while its size
 *  is unknown or the platform stages the files, and a full ring with a
 *  spinner inside while the target commits. The circle is the context
 *  gauge's (ChatStatusBar). */
export function MoveRing({ progress }: { progress: MoveProgress | null | undefined }) {
  if (!progress || !progress.total || progress.phase === MOVE_PHASE.STAGE) return <Spinner />
  const pct = Math.max(0, Math.min(100, Math.round((progress.done / progress.total) * 100)))
  const committing = progress.phase === MOVE_PHASE.COMMIT || pct >= 100
  return (
    <span className="relative inline-flex items-center justify-center w-4 h-4 text-link" data-testid="move-ring"
          data-pct={pct} aria-hidden="true">
      <svg className="w-4 h-4" viewBox="0 0 16 16">
        <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2" opacity="0.2" />
        <circle
          cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2"
          strokeDasharray={`${(pct / 100) * 37.7} 37.7`}
          strokeLinecap="round"
          transform="rotate(-90 8 8)"
        />
      </svg>
      {committing && (
        <span className="absolute inset-[5px] border-[1.5px] border-action border-t-transparent rounded-full animate-spin" />
      )}
    </span>
  )
}

/** How a machine is doing, in a word ('' for the platform). */
function stateWord(t: RunTarget): string {
  if (isLocalTarget(t.target)) return ''
  if (t.state === MACHINE_STATE.PAUSED) return 'paused'
  if (t.state === MACHINE_STATE.STALE) return 'slow connection'
  return t.reachable ? 'online' : 'offline'
}

/** A menu row's second line: a reason its dot does not already say. The
 *  server checks the guard, then the reach, then the engine, so a row the
 *  guard allows that cannot be picked while unreachable is offline, which
 *  the dot shows; a paused machine and a slow connection share the amber
 *  dot, so their word stays. */
function reasonLine(t: RunTarget & { is_current: boolean }): string {
  if (isLocalTarget(t.target)) return ''
  const blocked = !t.is_current && !t.pickable
  if (blocked && t.why_not && !(t.allowed && !t.reachable)) return t.why_not
  if (t.state === MACHINE_STATE.PAUSED || t.state === MACHINE_STATE.STALE) return stateWord(t)
  return ''
}

/** The row's dot: the platform is always up; a machine in the badge's
 *  colours (RemoteBadge), offline red whoever paired it. */
function dotClass(t: RunTarget): string {
  if (isLocalTarget(t.target)) return dotColorClass(MACHINE_STATE.ONLINE)
  return dotColorClass(t.state || (t.reachable ? MACHINE_STATE.ONLINE : MACHINE_STATE.DISCONNECTED))
}

function Tick() {
  return (
    <svg
      className="inline-block w-3 h-3 ml-1 -mt-0.5 align-middle text-link"
      data-testid="place-tick" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
    </svg>
  )
}

/** ``text`` with ``mark`` glued to its last character, so a title that
 *  wraps keeps the mark right of its last word and never starts a line
 *  with it alone. */
function Glued({ text, mark }: { text: string; mark: ReactNode }) {
  if (!mark) return <>{text}</>
  const chars = Array.from(text)
  const last = chars.pop() ?? ''
  return <>{chars.join('')}<span className="whitespace-nowrap">{last}{mark}</span></>
}

export default function RunsOnChip({ chatId, agentName, disabledReason, onMoved, engine, pickedPlace, onPickPlace }: Props) {
  if (!chatId) {
    return (
      <NewChatPill agentName={agentName} engine={engine ?? ''} picked={pickedPlace ?? ''}
                   disabledReason={disabledReason} onPick={onPickPlace} />
    )
  }
  // Keyed: a chat switch starts the pill afresh (no menu or confirm carried over).
  return <ChatPill key={chatId} chatId={chatId} disabledReason={disabledReason} onMoved={onMoved} />
}

/** A new chat: where it will start for this person, and the menu to pick
 *  another of the places its new chats may start. */
function NewChatPill({ agentName, engine, picked, disabledReason, onPick }: {
  agentName: string
  engine: string
  picked: string
  disabledReason: string
  onPick?: (target: string, isDefault: boolean) => void
}) {
  const { data } = useAgentRunTargets(agentName, undefined, undefined, engine || undefined)
  const { open, buttonRef, panelRef, style, portalRoot, toggle, close, onKeyDown } = usePortalMenu({ align: 'right' })
  // A pick the menu no longer offers (the computer was removed, or personal
  // computers were turned off) is dropped: the chat starts at the default.
  const stale = !!(picked && data && !data.targets.some((t) => t.target === picked && t.pickable))
  useEffect(() => {
    if (stale && data) onPick?.(data.default_target, true)
  }, [stale, data, onPick])
  if (!data) return null
  const start = (picked && data.targets.find((t) => t.target === picked))
    || data.targets.find((t) => t.is_default)
    || data.targets.find((t) => t.target === data.default_target)
  if (!start) return null
  const title = `Starts on ${start.label}`
  if (!onPick) {
    return <span className={PILL} data-testid="runs-on-chip" title={title} aria-label={title}><ComputerGlyph /></span>
  }
  const rows = data.targets.map((t) => ({ ...t, is_current: t.target === start.target }))
  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={toggle}
        disabled={!!disabledReason}
        title={disabledReason ? `${title}. ${disabledReason}` : title}
        aria-label={title}
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid="runs-on-chip"
        className={`${PILL} ${PILL_BUTTON}`}
      >
        <ComputerGlyph />
      </button>
      {open && createPortal(
        <PlacesPanel
          panelRef={panelRef}
          style={style}
          onKeyDown={onKeyDown}
          targets={rows}
          busy={false}
          onPick={(t) => { close(); onPick(t.target, t.target === data.default_target) }}
        />,
        portalRoot,
      )}
    </>
  )
}

function ChatPill({ chatId, disabledReason, onMoved }: { chatId: string; disabledReason: string; onMoved?: () => void }) {
  const { data } = useChatTargets(chatId)
  if (!data) return null
  const { current, moving } = data
  const face = moving ? <MoveRing progress={moving.progress} /> : <ComputerGlyph />
  const runsOn = `Runs on ${current.label}`
  // No menu: the server's sentence says why, in the tooltip and in the
  // note a tap opens.
  if (data.lock || !data.can_move) {
    const why = data.lock?.sentence || data.refusal
    return <PlaceNote lines={[runsOn, why]} className={`${PILL} ${PILL_BUTTON}`}>{face}</PlaceNote>
  }
  const title = moving ? `Moving to ${moving.to_label}…`
    : disabledReason ? `${runsOn}. ${disabledReason}` : runsOn
  return (
    <ChatTargetsMenu
      chatId={chatId}
      data={data}
      disabled={!!disabledReason}
      title={title}
      ariaLabel={moving ? title : runsOn}
      note={moving ? [title] : [runsOn, disabledReason]}
      className={`${PILL} ${PILL_BUTTON} ${PILL_RESTING}`}
      testId="runs-on-chip"
      onMoved={onMoved}
    >
      {face}
    </ChatTargetsMenu>
  )
}

/** A short read-only panel under a pill: where the chat runs and why its
 *  menu cannot open (the tooltip's words, for a screen with no hover). */
function NotePanel({ panelRef, style, onKeyDown, lines }: {
  panelRef: RefObject<HTMLDivElement | null>
  style: CSSProperties
  onKeyDown: (e: KeyboardEvent<HTMLElement>) => void
  lines: string[]
}) {
  return (
    <div
      ref={panelRef}
      role="note"
      data-testid="runs-on-note"
      onKeyDown={onKeyDown}
      style={style}
      className={`w-64 max-w-[calc(100vw-16px)] ${POPOVER} px-3 py-2 text-xs`}
    >
      {lines.map((line, i) => (
        <p key={line} className={i === 0 ? 'font-medium text-primary' : 'mt-1 text-tertiary break-words'}>{line}</p>
      ))}
    </div>
  )
}

/** A pill with no menu: its title on hover, its note on a click or a tap. */
function PlaceNote({ lines, className, children }: { lines: string[]; className: string; children: ReactNode }) {
  const { open, buttonRef, panelRef, style, portalRoot, toggle, onKeyDown } = usePortalMenu({ align: 'right' })
  const shown = lines.filter(Boolean)
  const title = shown.join('. ')
  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={toggle}
        title={title}
        aria-label={title}
        aria-expanded={open}
        data-testid="runs-on-chip"
        className={className}
      >
        {children}
      </button>
      {open && createPortal(
        <NotePanel panelRef={panelRef} style={style} onKeyDown={onKeyDown} lines={shown} />,
        portalRoot,
      )}
    </>
  )
}

/** The places as a menu panel: the current one checked, the default marked,
 *  a place that cannot be picked disabled with the server's sentence. */
function PlacesPanel({ panelRef, style, onKeyDown, targets, busy, onPick }: {
  panelRef: RefObject<HTMLDivElement | null>
  style: CSSProperties
  onKeyDown: (e: KeyboardEvent<HTMLElement>) => void
  targets: (RunTarget & { is_current: boolean })[]
  busy: boolean
  onPick: (t: RunTarget) => void
}) {
  return (
    <div
      ref={panelRef}
      role="menu"
      aria-label="Run this chat on"
      onKeyDown={onKeyDown}
      style={style}
      className={`w-64 max-w-[calc(100vw-16px)] max-h-80 overflow-y-auto ${POPOVER} py-1 text-xs`}
    >
      <div className="px-3 py-1.5 border-b border-subtle">
        <p className="text-3xs font-semibold text-tertiary uppercase tracking-wider">Run this chat on</p>
      </div>
      {targets.length === 0 && (
        <p className="px-3 py-2 text-tertiary">No other place can run this chat.</p>
      )}
      {targets.map((t) => {
        const blocked = !t.is_current && !t.pickable
        const second = reasonLine(t)
        const word = stateWord(t)
        const tick = t.is_current ? <Tick /> : null
        return (
          <button
            key={t.target}
            type="button"
            role="menuitem"
            disabled={t.is_current || blocked || busy}
            title={blocked && t.why_not ? t.why_not : undefined}
            onClick={() => onPick(t)}
            className="flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-surface-hover focus-visible:bg-surface-hover focus:outline-none disabled:cursor-default disabled:hover:bg-transparent"
          >
            <span
              data-testid="place-dot"
              aria-hidden="true"
              className={`mt-1 w-2 h-2 shrink-0 rounded-full ring-1 ring-border ${dotClass(t)}`}
            />
            <span className="min-w-0 flex-1">
              <span className={`block break-words ${
                t.is_current ? 'text-primary font-semibold' : blocked ? 'text-tertiary' : 'text-primary'}`}
              >
                <span data-place-label="">
                  <Glued text={t.label} mark={t.is_default ? null : tick} />
                </span>
                {t.is_default && (
                  <span className="font-normal text-tertiary">, <Glued text="default" mark={tick} /></span>
                )}
                {t.is_current && <span className="sr-only">, current</span>}
                {word && word !== second && <span className="sr-only">, {word}</span>}
              </span>
              {second && <span data-testid="place-reason" className="block text-3xs leading-snug text-tertiary break-words">{second}</span>}
            </span>
          </button>
        )
      })}
    </div>
  )
}

/**
 * A button that opens the chat's places as a menu, portaled and clamped to
 * the viewport (usePortalMenu), then moves the chat over HTTP after the
 * confirm. Disabled while `disabled` (a turn runs), while a move runs and
 * while the request is out; with a `note`, it opens the note instead then
 * (a touch screen has no tooltip). A refusal shows its sentence as a toast.
 * The caller renders it only when the answer allows a move (`can_move`, no
 * lock).
 */
export function ChatTargetsMenu({ chatId, data, disabled, title, ariaLabel, note, className, testId, onMoved, children }: {
  chatId: string
  data: ChatTargets
  disabled: boolean
  title: string
  ariaLabel?: string
  note?: string[]
  className: string
  testId?: string
  onMoved?: () => void
  children: ReactNode
}) {
  const { open, buttonRef, panelRef, style, portalRoot, toggle, close, onKeyDown } = usePortalMenu({ align: 'right' })
  const move = useMoveChat()
  const [picked, setPicked] = useState<RunTarget | null>(null)
  const busy = disabled || move.isPending || !!data.moving
  // The confirm goes for good when the move stops being possible (a turn
  // started, a move began elsewhere): it never comes back on its own.
  if (picked && (busy || !data.can_move || data.lock)) setPicked(null)

  // The focus goes back to the button, which the confirm returns it to.
  const pick = (t: RunTarget) => {
    close()
    setPicked(t)
  }
  const confirm = () => {
    if (!picked) return
    const target = picked.target
    setPicked(null)
    void move.mutateAsync({ chatId, target, from: data.current.target })
      .then(() => onMoved?.(), toastError('The chat was not moved'))
  }

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={toggle}
        disabled={busy && !note}
        aria-disabled={busy || undefined}
        title={title}
        aria-label={ariaLabel}
        aria-haspopup={busy && note ? undefined : 'menu'}
        aria-expanded={open}
        data-testid={testId}
        className={className}
      >
        {children}
      </button>
      {open && createPortal(
        busy && note
          ? <NotePanel panelRef={panelRef} style={style} onKeyDown={onKeyDown} lines={note.filter(Boolean)} />
          : <PlacesPanel panelRef={panelRef} style={style} onKeyDown={onKeyDown}
                         targets={data.targets} busy={busy} onPick={pick} />,
        portalRoot,
      )}
      {picked && (
        <MoveChatConfirm
          target={picked.target}
          label={picked.label}
          memory={moveMemoryOf(data, picked.target)}
          fromTarget={data.current.target}
          fromLabel={data.current.label}
          fromOffline={leavesOffline(data)}
          onConfirm={confirm}
          onCancel={() => setPicked(null)}
        />
      )}
    </>
  )
}

/** The chat runs on someone else's computer: read-only for this person,
 *  the server's sentence above the composer. */
export function ChatLockNotice({ sentence }: { sentence: string }) {
  if (!sentence) return null
  return (
    <div
      role="status"
      data-testid="chat-lock-notice"
      className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-control border border-status-info-border bg-status-info-bg text-status-info-fg"
    >
      {sentence}
    </div>
  )
}
