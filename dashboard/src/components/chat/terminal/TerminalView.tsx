import '@xterm/xterm/css/xterm.css'
import { useCallback } from 'react'
import { useInteractiveTerminal, type InteractiveWs } from '@/hooks/useInteractiveTerminal'
import { ptyPasteB64, withInteractiveTime } from '@/hooks/useInteractiveChat'
import { buildArtifactInteractionText } from '@/lib/artifactInteraction'
import type { useArtifactWindows } from '@/hooks/useArtifactWindows'
import PermissionDialog from '../PermissionDialog'
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
}

export default function TerminalView({ ws, chatId, className, agent, artifacts, onExit }: Props) {
  const { containerRef, pendingPermission, respondPermission, exited, exitReason, reconnecting, focus } =
    useInteractiveTerminal(ws, chatId, onExit)

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
    <div className={`relative flex-1 min-h-0 bg-p-bg overflow-hidden ${className || ''}`}>
      <div ref={containerRef} className="absolute inset-0" onMouseDown={focus} />
      <ArtifactWindows
        windows={artifacts.windows}
        minimized={artifacts.minimized}
        onClose={artifacts.close}
        onMinimize={artifacts.minimize}
        agent={agent}
        onArtifactInteraction={onArtifactInteraction}
      />

      {exited && (
        <div className="absolute top-2 right-3 z-10 text-xs font-medium text-white/90 bg-black/70 px-2 py-1 rounded-md shadow-xs">
          {exitReason === 'superseded'
            ? 'opened on another device'
            : exitReason === 'superseded_otodock'
              ? 'opened in a local terminal'
              : 'session ended'}
        </div>
      )}

      {/* satellite WS mid-reconnect — the session is held alive (grace),
          just frozen + input paused until it returns. */}
      {reconnecting && !exited && (
        <div className="absolute top-2 right-3 z-10 flex items-center gap-1.5 text-xs font-medium text-white/90 bg-amber-600/80 px-2 py-1 rounded-md shadow-xs">
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-white/90 animate-pulse" />
          reconnecting…
        </div>
      )}

      {pendingPermission && (
        <div className="absolute inset-x-0 bottom-0 z-20 p-3 bg-linear-to-t from-black/70 to-transparent">
          <div className="max-w-2xl mx-auto rounded-xl bg-p-bg/95 backdrop-blur-sm border border-p-border shadow-2xl px-3">
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
