import { useEffect, useState } from 'react'

import {
  useInstallSliceForChat,
  useInstallSliceByKey,
  useInstallSliceByAgent,
  type InstallSlice,
} from '../../store/installStore'
import { useDelayMount } from '../../hooks/useDelayMount'

interface Props {
  // Lookup precedence: chatId → (machineId+agent) → agent. chatId is
  // preferred for existing chats; the pair handles known new-chat targets;
  // agent-only is the new-chat-page fallback where execution_target is
  // only known once the first install event arrives (carries machine_id).
  chatId: string | null | undefined
  machineId?: string | null
  agent?: string | null
  onRetry?: () => void
}

// Render precedence (top-down, first match wins):
//  1. install.status === 'failed' → red banner with retry button
//  2. install.status === 'installing' → progress strip with elapsed-time hint
//  3. install.status === 'done' && elapsedSinceDone < 1500ms → grace "Install complete"
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
  const fromChat = useInstallSliceForChat(chatId)
  const fromKey = useInstallSliceByKey(machineId, agent)
  // Agent-only fallback fires only when no chat-keyed and no explicit-key
  // slice exists (i.e. new-chat page before warmup_ready provides target).
  const fromAgent = useInstallSliceByAgent(
    !fromChat && !fromKey ? agent : null,
  )
  const install = fromChat ?? fromKey ?? fromAgent

  // Render gating — anything we may want to show; final decision happens
  // after delay-mount + per-status checks below.
  const installShowing =
    install?.status === 'installing' ||
    install?.status === 'verifying' ||
    install?.status === 'failed'
  const installDoneRecent =
    install?.status === 'done' && install.mcps.length > 0 &&
    install.doneAt && Date.now() - install.doneAt < 1500
  const anyShowing = installShowing || installDoneRecent
  const visible = useDelayMount(!!anyShowing, 500)

  // Tick once per second so the elapsed-time hint refreshes without
  // requiring more events from the backend.
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!visible) return
    const t = setInterval(() => setTick((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [visible])

  // Hide the done-grace strip when it expires (re-render via tick handles it,
  // but in case ticks pause, do a fresh check on each render).
  if (!visible) return null

  // ── 1. install failed ──
  if (install?.status === 'failed') {
    return (
      <div className="w-full px-4 py-2 mx-auto max-w-4xl text-xs">
        <div className="flex items-center justify-between gap-3 px-3 py-2 rounded-sm border border-red-500/40 bg-red-500/10 text-red-600">
          <span className="truncate min-w-0">
            <strong>Install failed:</strong> {install.error ?? 'Unknown error.'}
          </span>
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="shrink-0 px-2 py-1 rounded-sm border border-red-500/40 hover:bg-red-500/20"
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
    const elapsedS = Math.floor((Date.now() - install.startedAt) / 1000)
    const showLongHint = elapsedS >= 30
    return (
      <div className="w-full px-4 py-2 mx-auto max-w-4xl text-xs text-p-text-light">
        <div className="flex flex-col gap-1">
          <div className="h-1 w-full bg-p-text-light/10 rounded-sm overflow-hidden">
            <div
              className="h-full bg-p-text-light/60 transition-[width] duration-300 ease-out"
              style={{ width: `${totalPct}%` }}
            />
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="truncate min-w-0">
              {currentMcp ? `Installing ${currentMcp}…` : 'Preparing remote environment…'}
            </span>
            <span className="shrink-0 tabular-nums opacity-60">{totalPct}%</span>
          </div>
          {install.failures.length > 0 && (
            <div className="flex flex-col gap-0.5 mt-1">
              {install.failures.map((f) => (
                <span key={f.mcp} className="text-red-500/80">
                  <span className="line-through">{f.mcp}</span>: install failed — session
                  will start without it
                </span>
              ))}
            </div>
          )}
          {showLongHint && (
            <div className="opacity-60 mt-1">
              First-time install on a fresh machine can take ~90s.
            </div>
          )}
        </div>
      </div>
    )
  }

  // ── 2b. post-install pre-warm boot check ──
  if (install?.status === 'verifying') {
    return (
      <div className="w-full px-4 py-2 mx-auto max-w-4xl text-xs text-p-text-light">
        <div className="flex flex-col gap-1">
          <div className="h-1 w-full bg-p-text-light/10 rounded-sm overflow-hidden">
            <div className="h-full w-full bg-p-text-light/60 animate-pulse" />
          </div>
          <span className="truncate min-w-0">Checking MCPs…</span>
        </div>
      </div>
    )
  }

  // ── 3. done grace window (only when MCPs were actually involved — an
  //       empty/no-op sync emits no lifecycle, but guard here too so it can
  //       never flash "100%" on an already-synced remote chat) ──
  if (install?.status === 'done' && install.mcps.length > 0 && install.doneAt && Date.now() - install.doneAt < 1500) {
    const warnCount = install.warmupFailures?.length ?? 0
    return (
      <div className="w-full px-4 py-2 mx-auto max-w-4xl text-xs text-p-text-light">
        <div className="flex items-center justify-between gap-3">
          <span className="truncate opacity-80">
            {warnCount > 0
              ? `Install complete — ${warnCount} MCP(s) need attention.`
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
