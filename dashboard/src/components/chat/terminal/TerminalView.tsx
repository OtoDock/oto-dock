import '@xterm/xterm/css/xterm.css'
import { useCallback } from 'react'
import { useInteractiveTerminal, type InteractiveWs } from '@/hooks/useInteractiveTerminal'
import { ptyPasteB64, withInteractiveTime } from '@/hooks/useInteractiveChat'
import { buildArtifactInteractionText } from '@/lib/artifactInteraction'
import type { useArtifactWindows } from '@/hooks/useArtifactWindows'
import PermissionDialog from '../PermissionDialog'
import ArtifactWindows from '../artifacts/ArtifactWindows'
import Tooltip from '../../ui/Tooltip'
import { DONT_ASK_CHIP, runsWithoutAsking } from '../../../lib/engines/modes'

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
  /** The chat's permission mode. The TUI prints its own mode line for Ask,
   * Auto and Plan, so the badge shows only Don't Ask: the CLI has no Don't
   * Ask of its own (the spawn maps it to the CLI's default), so its footer
   * reads "manual mode" while the platform runs every tool. The mode
   * follows a Shift+Tab inside the terminal. */
  permissionMode?: string
}

/** The badge: Don't Ask alone, the mode the terminal cannot show. */
const DONT_ASK_BADGE = {
  note: "Don't Ask: this chat runs every tool without asking. The terminal's own mode line does not show it.",
  classes: `${DONT_ASK_CHIP.bg} ${DONT_ASK_CHIP.border} ${DONT_ASK_CHIP.text}`,
  path: 'M13 10V3L4 14h7v7l9-11h-7z',
}

function badgeOf(mode: string | undefined) {
  return runsWithoutAsking(mode) ? DONT_ASK_BADGE : undefined
}

export default function TerminalView({ ws, chatId, className, agent, artifacts, onExit, permissionMode }: Props) {
  const badge = badgeOf(permissionMode)
  const {
    containerRef, pendingPermission, respondPermission, exited, exitReason,
    reconnecting, focus, readOnly, controller, takeOver,
  } = useInteractiveTerminal(ws, chatId, onExit)

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
    <div className={`relative flex-1 min-h-0 bg-surface-page overflow-hidden ${className || ''}`}>
      <div ref={containerRef} className="absolute inset-0" onMouseDown={focus} />
      <ArtifactWindows
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
        {/* The status bar's mode chip, the glyph alone: its tint is
            translucent, so it sits on the page colour, never on the
            terminal's text. It takes focus, so a keyboard reaches the note. */}
        {badge && (
          <Tooltip text={badge.note}>
            <span className="block rounded-control bg-surface-page shadow-elevation-1">
              <span
                role="img"
                tabIndex={0}
                aria-label={badge.note}
                data-testid="terminal-mode"
                className={`flex items-center justify-center w-7 h-7 rounded-control border ${badge.classes} focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring/50`}
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={badge.path} />
                </svg>
              </span>
            </span>
          </Tooltip>
        )}

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
  )
}
