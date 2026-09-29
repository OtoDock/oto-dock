import { Link } from 'react-router-dom'
import { SYSTEM_SUBTYPE } from '../../api/wireEvents'

interface Props {
  subtype: string
  agentName?: string
  agentColor?: string
  message?: string
}

// The centered-divider subtypes: the two the history renderer synthesises
// from the nudge rows, the self-wake marker, and three meeting outcomes.
const LABELS: Record<string, string> = {
  [SYSTEM_SUBTYPE.BG_AGENTS_COMPLETED]: 'Background agents completed',
  [SYSTEM_SUBTYPE.BG_COMMANDS_COMPLETED]: 'Background commands completed',
  // Provenance marker before an unprompted self-wake review turn (the
  // engine wakes itself when background work finishes — 1.5).
  [SYSTEM_SUBTYPE.BG_WAKE]: 'Background work finished — the agent reviews it',
  [SYSTEM_SUBTYPE.MEETING_CONCLUDED]: 'Meeting concluded',
  [SYSTEM_SUBTYPE.MEETING_AGENT_FAILED]: 'Agent disconnected from meeting',
  [SYSTEM_SUBTYPE.MEETING_AGENT_LEFT]: 'Agent left the meeting',
}

export default function SystemEvent({ subtype, message }: Props) {
  // Meeting turn start/end: no inline separators (indicator bar handles speaker identity)
  if (subtype === SYSTEM_SUBTYPE.MEETING_TURN_START) return null
  if (subtype === SYSTEM_SUBTYPE.MEETING_TURN_END) return null

  // Auto-continued with a fresh session: the chat's pinned
  // machine was deleted (or its session files aged out), so the proxy
  // spawned a new session seeded from DB history. Persisted — renders on
  // live push and on every reload at the discontinuity point.
  if (subtype === SYSTEM_SUBTYPE.SESSION_RESEEDED) {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-900/40 text-sm text-blue-800 dark:text-blue-300">
        <div className="font-medium">Continued with a fresh session</div>
        {message && <div className="mt-1 opacity-90">{message}</div>}
      </div>
    )
  }

  // No usable subscription for this execution layer (user-scoped warmup blocked).
  // A setup prompt, not a crash — amber, points the user at their settings.
  if (subtype === SYSTEM_SUBTYPE.NO_SUBSCRIPTION) {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-900/40 text-sm text-amber-800 dark:text-amber-300">
        <div className="font-medium">Subscription required</div>
        {message && <div className="mt-1 opacity-90">{message}</div>}
      </div>
    )
  }

  // The pool's subscription cap refused the spawn (the proxy's wording says
  // which cap and where it is set). Amber: a limit the user or an admin
  // chose, not a crash.
  if (subtype === SYSTEM_SUBTYPE.POOL_CAP) {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-900/40 text-sm text-amber-800 dark:text-amber-300">
        <div className="font-medium">Subscription cap reached</div>
        {message && <div className="mt-1 opacity-90">{message}</div>}
        <div className="mt-1 text-xs">
          <Link to="/user-settings?tab=usage" className="underline hover:no-underline">Open the Usage tab</Link>
        </div>
      </div>
    )
  }

  // Remote target unreachable — session refused to start. Shown in place
  // of the assistant placeholder bubble so the user gets clear feedback
  // instead of a silently-vanishing message.
  if (subtype === SYSTEM_SUBTYPE.TARGET_UNAVAILABLE) {
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
  if (subtype === SYSTEM_SUBTYPE.SESSION_ERROR) {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/40 text-sm text-red-800 dark:text-red-300">
        <div className="font-medium">Couldn’t start the session</div>
        {message && <div className="mt-1 opacity-90">{message}</div>}
      </div>
    )
  }

  // Meeting never started (admission denial, spawn failure) — red card with
  // the orchestrator's reason; the meeting pill is cleared by the handler.
  if (subtype === SYSTEM_SUBTYPE.MEETING_FAILED) {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/40 text-sm text-red-800 dark:text-red-300">
        <div className="font-medium">Meeting could not start</div>
        {message && <div className="mt-1 opacity-90">{message}</div>}
      </div>
    )
  }

  // Meeting started: branded banner
  if (subtype === SYSTEM_SUBTYPE.MEETING_STARTED) {
    return (
      <div className="flex items-center justify-center gap-2 py-3 my-2 text-xs">
        <span className="h-px flex-1 bg-[#0891b2]/30" />
        <span className="text-[#0891b2] font-medium px-2">Meeting started</span>
        <span className="h-px flex-1 bg-[#0891b2]/30" />
      </div>
    )
  }

  // Undelivered input: the server buffered text for a starting terminal and
  // could not confirm it became a turn (a TUI dialog may have swallowed it).
  // Full text shown un-truncated so the user can copy their prompt back.
  if (subtype === SYSTEM_SUBTYPE.UNDELIVERED_INPUT) {
    return (
      <div className="my-2 px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-900/40 text-sm text-amber-800 dark:text-amber-300">
        <div className="font-medium">This message may not have reached the agent</div>
        {message && (
          <div className="mt-1 whitespace-pre-wrap break-words opacity-90 select-text">{message}</div>
        )}
        <div className="mt-1 text-xs opacity-70">
          It was typed while the session was starting. If the agent never answered it, copy it and send again.
        </div>
      </div>
    )
  }

  // Context compressed: amber separator
  if (subtype === SYSTEM_SUBTYPE.CONTEXT_COMPRESSED) {
    return (
      <div className="flex items-center justify-center gap-2 py-3 my-2 text-xs">
        <span className="h-px flex-1 bg-[#b8860b]/30" />
        <span className="text-[#b8860b] font-medium px-2">Context compressed</span>
        <span className="h-px flex-1 bg-[#b8860b]/30" />
      </div>
    )
  }

  // Meeting concluded: branded banner
  if (subtype === SYSTEM_SUBTYPE.MEETING_CONCLUDED) {
    return (
      <div className="flex items-center justify-center gap-2 py-3 my-2 text-xs">
        <span className="h-px flex-1 bg-brand/30" />
        <span className="text-brand font-medium px-2">Meeting concluded</span>
        <span className="h-px flex-1 bg-brand/30" />
      </div>
    )
  }

  const label = LABELS[subtype]
  // Unknown subtypes (the CLI's raw pass-through such as "api_retry", a
  // subtype a newer proxy mints) are suppressed — rendering raw subtype
  // strings as separators is confusing. The proxy's translator filters the
  // heartbeats on the server too; this is the defense-in-depth layer.
  if (!label) return null

  return (
    <div className="flex items-center justify-center gap-2 py-1 my-1 text-xs text-p-text-light">
      <span className="h-px flex-1 bg-p-border-light" />
      <span>{label}</span>
      <span className="h-px flex-1 bg-p-border-light" />
    </div>
  )
}
