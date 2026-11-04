import { useEffect, useState } from 'react'

import {
  useInstallSliceForChat,
  useInstallSliceByKey,
  useInstallSliceByAgent,
  type InstallSlice,
} from '../../store/installStore'
import { useChatStore } from '../../store/chatStore'
import { useDelayMount } from '../../hooks/useDelayMount'

// The proxy's row for the session start's workspace sync. A sync alone ends
// with no grace strip: only an install or update earns "Install complete".
const WORKSPACE_ROW = 'workspace files'

interface Props {
  // A chat shows the install of the computer it runs on and nothing else:
  // its own target from the chat store (the warm-up names it, "" while the
  // start has not placed it). The page's session target (`machineId`) only
  // on the new-chat page: a chat's may still be the previous chat's. The
  // agent's latest install only while the target is unknown.
  chatId: string | null | undefined
  machineId?: string | null
  agent?: string | null
  onRetry?: () => void
}

// Render precedence (top-down, first match wins):
//  1. install.status === 'failed' → red banner with retry button
//  2. install.status === 'installing' → progress strip with elapsed-time hint
//  3. install.status === 'done' && elapsedSinceDone < 1500ms → grace "Install complete",
//     unless the session started with tools still installing (the chat's
//     line names them): the bar then goes with the session start
//  4. else → nothing
//
// Warmup failures (offline machine, session-start error) are NOT rendered
// here — the target_unavailable system card in the message list is the
// single error surface (AgentChat sets
// appendErrorOnEmptyWarmupFail so the card also covers an empty new chat).
//
// Mounted ABOVE the chat input bar (input stays enabled during install;
// messages typed mid-install queue locally). Delay-mounted 500ms to
// suppress the flash on cached-MCP install (which completes in <100ms).
export default function InstallProgressBar({ chatId, machineId, agent, onRetry }: Props) {
  const chatTarget = useChatStore((s) => (chatId ? s.byChat[chatId]?.executionTarget ?? '' : ''))
  const fromChat = useInstallSliceForChat(chatId)
  const fromKey = useInstallSliceByKey(chatId ? null : machineId, agent)
  const targetKnown = chatId ? chatTarget !== '' : !!machineId
  const fromAgent = useInstallSliceByAgent(targetKnown ? null : agent)
  const install = fromChat ?? fromKey ?? fromAgent
  // The clock the elapsed hint and the done-grace window read, advanced by
  // the tick below while the bar shows.
  const [now, setNow] = useState(() => Date.now())

  // Render gating — anything we may want to show; final decision happens
  // after delay-mount + per-status checks below.
  const installShowing =
    install?.status === 'installing' ||
    install?.status === 'verifying' ||
    install?.status === 'failed'
  const installedMcps = !!install?.mcps.some((m) => m !== WORKSPACE_ROW)
  const installDoneRecent =
    install?.status === 'done' && installedMcps && !install.stillInstalling?.length &&
    install.doneAt && now - install.doneAt < 1500
  const anyShowing = installShowing || installDoneRecent
  const visible = useDelayMount(!!anyShowing, 500)

  // Tick once per second so the elapsed-time hint refreshes without
  // requiring more events from the backend.
  useEffect(() => {
    if (!visible) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [visible])

  // Hide the done-grace strip when it expires (re-render via tick handles it,
  // but in case ticks pause, do a fresh check on each render).
  if (!visible) return null

  // ── 1. install failed ──
  if (install?.status === 'failed') {
    return (
      <div className="w-full px-4 py-2 mx-auto max-w-4xl text-xs">
        <div className="flex items-center justify-between gap-3 px-3 py-2 rounded-sm border border-status-danger-solid bg-status-danger-bg text-status-danger-fg">
          <span className="truncate min-w-0">
            <strong>Install failed:</strong> {install.error ?? 'Unknown error.'}
          </span>
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="shrink-0 px-2 py-1 rounded-sm border border-status-danger-solid hover:bg-status-danger-bg"
            >
              Retry
            </button>
          )}
        </div>
      </div>
    )
  }

  // ── 2. install in progress ──
  if (install?.status === 'installing') {
    const totalPct = computeAggregatePct(install)
    const currentMcp = pickActiveMcp(install)
    const elapsedS = Math.max(0, Math.floor((now - install.startedAt) / 1000))
    const showLongHint = elapsedS >= 30
    const copying = currentMcp === WORKSPACE_ROW
    const files = install.progress[WORKSPACE_ROW]?.filesTotal ?? 0
    return (
      <div className="w-full px-4 py-2 mx-auto max-w-4xl text-xs text-tertiary">
        <div className="flex flex-col gap-1">
          <div className="h-1 w-full bg-surface-muted rounded-sm overflow-hidden">
            <div
              className="h-full bg-status-info-solid transition-[width] duration-300 ease-out"
              style={{ width: `${totalPct}%` }}
            />
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="truncate min-w-0">
              {copying ? 'Syncing workspace files…' : currentMcp ? `Installing ${currentMcp}…` : 'Preparing remote environment…'}
            </span>
            <span className="shrink-0 tabular-nums opacity-60">{totalPct}%</span>
          </div>
          {install.failures.length > 0 && (
            <div className="flex flex-col gap-0.5 mt-1">
              {install.failures.map((f) => (
                <span key={f.mcp} className="text-status-danger-fg">
                  <span className="line-through">{f.mcp}</span>: install failed. The chat
                  will start without it.
                </span>
              ))}
            </div>
          )}
          {showLongHint && (
            <div className="opacity-60 mt-1">
              {!copying
                ? 'The first install on a new machine can take about 90 seconds.'
                : files > 0
                  ? `Copying ${files.toLocaleString()} workspace files to this computer. A large copy can take several minutes.`
                  : 'Copying the workspace files to this computer. A large copy can take several minutes.'}
            </div>
          )}
        </div>
      </div>
    )
  }

  // ── 2b. post-install pre-warm boot check ──
  if (install?.status === 'verifying') {
    return (
      <div className="w-full px-4 py-2 mx-auto max-w-4xl text-xs text-tertiary">
        <div className="flex flex-col gap-1">
          <div className="h-1 w-full bg-surface-muted rounded-sm overflow-hidden">
            <div className="h-full w-full bg-status-info-solid animate-pulse" />
          </div>
          <span className="truncate min-w-0">Checking MCPs…</span>
        </div>
      </div>
    )
  }

  // ── 3. done grace window (only when MCPs were actually involved — an
  //       empty/no-op sync emits no lifecycle, and a workspace sync alone
  //       is not an install, so it never flashes "100%") ──
  if (install?.status === 'done' && installedMcps && !install.stillInstalling?.length &&
      install.doneAt && now - install.doneAt < 1500) {
    const warnCount = install.warmupFailures?.length ?? 0
    return (
      <div className="w-full px-4 py-2 mx-auto max-w-4xl text-xs text-tertiary">
        <div className="flex items-center justify-between gap-3">
          <span className="truncate opacity-80">
            {warnCount > 0
              ? `Install complete. ${warnCount} MCP(s) need attention.`
              : 'Install complete.'}
          </span>
          <span className="shrink-0 tabular-nums opacity-60">100%</span>
        </div>
      </div>
    )
  }

  return null
}

function computeAggregatePct(install: InstallSlice): number {
  if (install.mcps.length === 0) return 0
  let total = 0
  for (const name of install.mcps) {
    const entry = install.progress[name]
    total += entry?.pct ?? 0
  }
  return Math.min(100, Math.max(0, Math.round(total / install.mcps.length)))
}

function pickActiveMcp(install: InstallSlice): string | null {
  // Most recently progressed MCP that hasn't finished yet. Heuristic: last
  // entry in progress whose phase is not 'done' or 'failed'.
  let lastActive: string | null = null
  for (const name of install.mcps) {
    const entry = install.progress[name]
    if (!entry) continue
    if (entry.phase !== 'done' && entry.phase !== 'failed') {
      lastActive = name
    }
  }
  return lastActive
}
