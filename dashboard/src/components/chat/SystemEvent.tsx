interface Props {
  subtype: string
  agentName?: string
  agentColor?: string
  // Optional structured payload for progress-style events.
  mcpName?: string
  progressPct?: number
  phase?: string
  message?: string
}

const LABELS: Record<string, string> = {
  bg_monitoring: 'Monitoring background agents...',
  bg_agents_complete: 'Background agents completed',
  delegate_completed: 'Delegated task completed',
  bg_agents_completed: 'Background agents completed',
  bg_commands_completed: 'Background commands completed',
  meeting_concluded: 'Meeting concluded',
  meeting_agent_failed: 'Agent disconnected from meeting',
  meeting_agent_left: 'Agent left the meeting',
}

export default function SystemEvent({
  subtype,
  mcpName, progressPct, phase, message,
}: Props) {
  // Meeting turn start/end: no inline separators (indicator bar handles speaker identity)
  if (subtype === 'meeting_turn_start') return null
  if (subtype === 'meeting_turn_end') return null

  // MCP install progress — amber banner with pct bar, auto-replaced when
  // the first assistant message arrives. The proxy emits these during the
  // sync_mcps pass before a remote session starts.
  if (subtype === 'mcp_installation_progress') {
    const pct = Math.max(0, Math.min(100, progressPct ?? 0))
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-900/40">
        <div className="flex items-center justify-between text-xs text-amber-800 dark:text-amber-300 mb-1">
          <span>Installing MCP{mcpName ? `: ${mcpName}` : '...'}</span>
          <span className="text-amber-600 dark:text-amber-400">{phase || ''} {pct}%</span>
        </div>
        <div className="h-1 rounded-full bg-amber-200 dark:bg-amber-900/40 overflow-hidden">
          <div className="h-1 bg-amber-500" style={{ width: `${pct}%` }} />
        </div>
        {message && (
          <div className="text-xs text-amber-700 dark:text-amber-400 mt-1 truncate">{message}</div>
        )}
      </div>
    )
  }

  // MCP install failed — red warning card, session continues without MCP.
  if (subtype === 'mcp_install_failed') {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/40 text-xs text-red-800 dark:text-red-300">
        <div className="font-medium">MCP install failed{mcpName ? `: ${mcpName}` : ''}</div>
        {message && <div className="mt-1 opacity-80 truncate">{message}</div>}
        <div className="mt-1 opacity-70">Session will run without this MCP.</div>
      </div>
    )
  }

  // Auto-continued with a fresh session: the chat's pinned
  // machine was deleted (or its session files aged out), so the proxy
  // spawned a new session seeded from DB history. Persisted — renders on
  // live push and on every reload at the discontinuity point.
  if (subtype === 'session_reseeded') {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-900/40 text-sm text-blue-800 dark:text-blue-300">
        <div className="font-medium">Continued with a fresh session</div>
        {message && <div className="mt-1 opacity-90">{message}</div>}
      </div>
    )
  }

  // No usable subscription for this execution layer (user-scoped warmup blocked).
  // A setup prompt, not a crash — amber, points the user at their settings.
  if (subtype === 'no_subscription') {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-900/40 text-sm text-amber-800 dark:text-amber-300">
        <div className="font-medium">Subscription required</div>
        {message && <div className="mt-1 opacity-90">{message}</div>}
      </div>
    )
  }

  // Remote target unreachable — session refused to start. Shown in place
  // of the assistant placeholder bubble so the user gets clear feedback
  // instead of a silently-vanishing message.
  if (subtype === 'target_unavailable') {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/40 text-sm text-red-800 dark:text-red-300">
        <div className="font-medium">Remote machine unavailable</div>
        {message && <div className="mt-1 opacity-90">{message}</div>}
      </div>
    )
  }

  // Session failed to START on a reachable machine (config/spawn error — e.g.
  // a bad config.toml). NOT an availability problem, so a distinct title from
  // 'target_unavailable'; carries the backend's error for diagnosis.
  if (subtype === 'session_error') {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/40 text-sm text-red-800 dark:text-red-300">
        <div className="font-medium">Couldn’t start the session</div>
        {message && <div className="mt-1 opacity-90">{message}</div>}
      </div>
    )
  }

  // Meeting never started (admission denial, spawn failure) — red card with
  // the orchestrator's reason; the meeting pill is cleared by the handler.
  if (subtype === 'meeting_failed') {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/40 text-sm text-red-800 dark:text-red-300">
        <div className="font-medium">Meeting could not start</div>
        {message && <div className="mt-1 opacity-90">{message}</div>}
      </div>
    )
  }

  // Meeting started: branded banner
  if (subtype === 'meeting_started') {
    return (
      <div className="flex items-center justify-center gap-2 py-3 my-2 text-xs">
        <span className="h-px flex-1 bg-[#0891b2]/30" />
        <span className="text-[#0891b2] font-medium px-2">Meeting started</span>
        <span className="h-px flex-1 bg-[#0891b2]/30" />
      </div>
    )
  }

  // Context compressed: amber separator
  if (subtype === 'context_compressed') {
    return (
      <div className="flex items-center justify-center gap-2 py-3 my-2 text-xs">
        <span className="h-px flex-1 bg-[#b8860b]/30" />
        <span className="text-[#b8860b] font-medium px-2">Context compressed</span>
        <span className="h-px flex-1 bg-[#b8860b]/30" />
      </div>
    )
  }

  // Meeting concluded: branded banner
  if (subtype === 'meeting_concluded') {
    return (
      <div className="flex items-center justify-center gap-2 py-3 my-2 text-xs">
        <span className="h-px flex-1 bg-brand/30" />
        <span className="text-brand font-medium px-2">Meeting concluded</span>
        <span className="h-px flex-1 bg-brand/30" />
      </div>
    )
  }

  const label = LABELS[subtype]
  // Unknown subtypes (e.g. CLI "status" heartbeats, "api_retry", etc.) are
  // suppressed — rendering raw subtype strings as separators is confusing.
  // The proxy's translator filters these on the server too; this is the
  // defense-in-depth layer for any subtypes that slip through.
  if (!label) return null

  return (
    <div className="flex items-center justify-center gap-2 py-1 my-1 text-xs text-p-text-light">
      <span className="h-px flex-1 bg-p-border-light" />
      <span>{label}</span>
      <span className="h-px flex-1 bg-p-border-light" />
    </div>
  )
}
