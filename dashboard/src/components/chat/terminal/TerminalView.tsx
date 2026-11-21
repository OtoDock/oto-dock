import '@xterm/xterm/css/xterm.css'
import { useCallback, useState } from 'react'
import { useInteractiveTerminal, type InteractiveWs } from '@/hooks/useInteractiveTerminal'
import { ptyPasteB64, withInteractiveTime } from '@/hooks/useInteractiveChat'
import { buildArtifactInteractionText } from '@/lib/artifactInteraction'
import type { useArtifactWindows } from '@/hooks/useArtifactWindows'
import PermissionDialog from '../PermissionDialog'
import SystemEvent from '../SystemEvent'
import { limitCardKey, type SystemBlock } from '@/lib/messageBlocks'
import { loadDismissed, recordDismissed } from '@/lib/chatNoticeDismissals'
import ArtifactWindows from '../artifacts/ArtifactWindows'

/**
 * The live view of an interactive CLI (PTY) session: a
 * themed xterm.js terminal mirroring the native Claude TUI, with platform
 * permission prompts surfaced as a floating overlay (reusing PermissionDialog)
 * and display/file-tools artifacts as floating PiP windows. Mounted by
 * AgentChat only when the session is interactive; lazy-loaded so xterm stays
 * out of the main bundle.
 */
interface Props {
  ws: InteractiveWs
  chatId: string
  className?: string
  /** Chat's agent slug — ui artifact windows live-reload on a display_ui rewrite. */
  agent?: string
  /** Display/file-tools artifact windows. Lifted to the
   * page so the minimized dock (ArtifactDock) can live in the page's top-left
   * panel stack; the OPEN windows float here over the terminal. */
  artifacts: ReturnType<typeof useArtifactWindows>
  /** Fired when the PTY process REALLY ends (not 'superseded') — the page leaves
   * the dead terminal for the DB rich view + primes a resume on the next send. */
  onExit?: () => void
  /** The chat's usage-limit card (`terminalLimitCard`): shown under the
   *  terminal, never over its last rows, while no permission card is and
   *  until a person hides that limit's card in this chat. */
  notice?: SystemBlock
}

export default function TerminalView({ ws, chatId, className, agent, artifacts, onExit, notice }: Props) {
  const {
    containerRef, pendingPermission, respondPermission, exited, exitReason,
    reconnecting, focus, readOnly, controller, takeOver,
  } = useInteractiveTerminal(ws, chatId, onExit)
  // The limit cards a person hid in this chat, kept by this browser.
  const [hidden, setHidden] = useState(() => loadDismissed('limitcard', chatId))
  // The chat switch reloads them, in render (as ChatTargetBanner does).
  const [hiddenFor, setHiddenFor] = useState(chatId)
  if (hiddenFor !== chatId) {
    setHiddenFor(chatId)
    setHidden(loadDismissed('limitcard', chatId))
  }
  const limitKey = notice ? limitCardKey(notice) : ''
  const showNotice = !!notice && !pendingPermission && !hidden.has(limitKey)

  // Backchannel for PiP artifacts: interactive chats deliver by TYPING the
  // framed interaction into the terminal — the composer's own rail (bracketed
  // paste, composer flag so a parked question picker holds it, time-stamped
  // at delivery). The injected text is visible in the terminal, so the user
  // sees exactly what the artifact sent; UiArtifact's consent chip + client
  // rate limit gate it like everywhere else.
  const onArtifactInteraction = useCallback(
    async (_token: string, title: string, payload: unknown) => {
      if (exited) return { status: 'unavailable', reason: 'session ended' }
      const built = buildArtifactInteractionText(title, payload)
      if ('error' in built) return { status: 'denied', reason: built.error }
      ws.sendPtyInput(chatId, ptyPasteB64(withInteractiveTime(built.framed)), true)
      return { status: 'sent' }
    },
    [ws, chatId, exited],
  )

  // plan_review/question get a richer native UI later; for now they render
  // as allow/deny so the agent never hangs.
  const permLabel =
    pendingPermission && pendingPermission.kind !== 'permission'
      ? `${pendingPermission.toolName || pendingPermission.kind} (${pendingPermission.kind})`
      : pendingPermission?.toolName || ''

  return (
    <div className={`flex-1 min-h-0 flex flex-col bg-surface-page overflow-hidden ${className || ''}`}>
      <div className="relative flex-1 min-h-0">
      <div ref={containerRef} className="absolute inset-0" onMouseDown={focus} />
      <ArtifactWindows
        chatId={chatId}
        windows={artifacts.windows}
        minimized={artifacts.minimized}
        onClose={artifacts.close}
        onMinimize={artifacts.minimize}
        agent={agent}
        onArtifactInteraction={onArtifactInteraction}
      />

      {/* One column for the terminal's badges, so none covers another (the
          read-only one carries its Take over button). Absolute: the
          terminal's size never depends on them. */}
      <div className="absolute top-2 right-3 z-10 flex flex-col items-end gap-1.5">
        {exited && (
          <div className="text-xs font-medium text-status-info-fg bg-status-info-bg border border-status-info-border px-2 py-1 rounded-control shadow-elevation-1">
            {exitReason === 'superseded'
              ? 'Opened on another device'
              : exitReason === 'superseded_otodock'
                ? 'Opened in a local terminal'
                : 'Session ended'}
          </div>
        )}

        {/* Another user's terminal (Shared-only agents share one chat): it runs
            under THEIR account and platform role, so it can be watched but not
            driven. Taking over ends their session and continues the same
            conversation under this user on the next send. */}
        {readOnly && !exited && (
          <div className="flex items-center gap-2 text-xs font-medium text-status-info-fg bg-status-info-bg border border-status-info-border px-2 py-1 rounded-control shadow-elevation-1">
            <span>{controller ? `${controller} is driving` : 'read-only'}</span>
            <button
              type="button"
              onClick={takeOver}
              className="rounded-sm border border-current px-1.5 py-0.5 hover:underline"
            >
              Take over
            </button>
          </div>
        )}

        {/* satellite WS mid-reconnect — the session is held alive (grace),
            just frozen + input paused until it returns. */}
        {reconnecting && !exited && (
          <div className="flex items-center gap-1.5 text-xs font-medium text-status-warning-fg bg-status-warning-bg border border-status-warning-border px-2 py-1 rounded-control shadow-elevation-1">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-status-warning-solid animate-pulse" />
            reconnecting…
          </div>
        )}
      </div>

      {pendingPermission && (
        // The gradient is decoration: clicks beside the card go through to
        // what is under it (the terminal, a composer popup rising into it).
        <div className="pointer-events-none absolute inset-x-0 bottom-0 z-20 p-3 bg-linear-to-t from-surface-page to-transparent">
          <div className="pointer-events-auto max-w-2xl mx-auto rounded-dialog bg-surface-overlay shadow-elevation-4 px-3">
            <PermissionDialog
              requestId={pendingPermission.requestId}
              toolName={permLabel}
              toolInput={pendingPermission.toolInput}
              onRespond={respondPermission}
            />
          </div>
        </div>
      )}
      </div>

      {/* Its own strip: the terminal refits above it, so the engine's own
          rows and input line stay in view. */}
      {showNotice && notice && (
        <div className="shrink-0 px-3 pt-2" data-testid="terminal-limit-card">
          <div className="max-w-2xl mx-auto">
            <SystemEvent
              subtype={notice.subtype}
              message={notice.message}
              reason={notice.reason}
              onDismiss={() => {
                // Hidden for this view even when the browser refuses the
                // write. The store decides the next mount.
                recordDismissed('limitcard', chatId, limitKey)
                setHidden((was) => new Set(was).add(limitKey))
              }}
            />
          </div>
        </div>
      )}
    </div>
  )
}
