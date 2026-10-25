import { Link } from 'react-router-dom'
import { CHAT_MOVED_REASON, RESEND_REASONS, SYSTEM_SUBTYPE, UNDELIVERED_REASON } from '../../api/wireEvents'

interface Props {
  subtype: string
  agentName?: string
  agentColor?: string
  message?: string
  /** The row's `reason`, when the subtype has more than one cause. */
  reason?: string
  /** A refused send's card: whether what was sent is back in the composer. */
  returned?: boolean
  /** The turn-ended card's button, from the transcript
   *  (`lib/messageBlocks.sendAgainKind`): Resume once the turn had started,
   *  Send again when nothing ran, none while a turn streams or for an
   *  ending no re-send answers. */
  sendAgain?: 'resume' | 'again'
  onSendAgain?: () => void
}

// The proxy's line ends in an instruction for a reader without this card
// (a task run's output, a delegator). The card says its own, so it drops it.
const LINE_INSTRUCTIONS = [
  'Send the message again once the machine is back.',
  'Send the message again, the conversation continues where it left off.',
  'Send the message again.',
  'Send a message to continue.',
  'Send it again.',
]
function withoutInstruction(message?: string): string | undefined {
  if (!message) return message
  const text = message.trimEnd()
  const tail = LINE_INSTRUCTIONS.find((s) => text.endsWith(s))
  return tail ? text.slice(0, -tail.length).trimEnd() : message
}

// A notice in the transcript: the status pill's tint, edge and foreground
// in one box (its body text stays at full strength: a dimmed foreground on
// a tint falls under AA).
const NOTICE_BOX = 'my-2 px-3 py-2 rounded-card border text-sm'
const NOTICE = {
  info: 'bg-status-info-bg border-status-info-border text-status-info-fg',
  warning: 'bg-status-warning-bg border-status-warning-border text-status-warning-fg',
  danger: 'bg-status-danger-bg border-status-danger-border text-status-danger-fg',
  neutral: 'bg-surface-muted border-subtle text-secondary',
} as const

// The centered-divider subtypes: the two the history renderer synthesises
// from the nudge rows, the self-wake marker, and three meeting outcomes.
const LABELS: Record<string, string> = {
  [SYSTEM_SUBTYPE.BG_AGENTS_COMPLETED]: 'Background agents completed',
  [SYSTEM_SUBTYPE.BG_COMMANDS_COMPLETED]: 'Background commands completed',
  // Provenance marker before an unprompted self-wake review turn (the
  // engine wakes itself when background work finishes — 1.5).
  [SYSTEM_SUBTYPE.BG_WAKE]: 'Background work finished: the agent reviews it',
  [SYSTEM_SUBTYPE.MEETING_CONCLUDED]: 'Meeting concluded',
  [SYSTEM_SUBTYPE.MEETING_AGENT_FAILED]: 'Agent disconnected from meeting',
  [SYSTEM_SUBTYPE.MEETING_AGENT_LEFT]: 'Agent left the meeting',
}

export default function SystemEvent({ subtype, message, reason, returned, sendAgain, onSendAgain }: Props) {
  // Meeting turn start/end: no inline separators (indicator bar handles speaker identity)
  if (subtype === SYSTEM_SUBTYPE.MEETING_TURN_START) return null
  if (subtype === SYSTEM_SUBTYPE.MEETING_TURN_END) return null

  // The turn ended other than by its own result: the ending's line (what
  // happened), persisted by the proxy inside the turn so a reload shows it
  // where the live frame did, then the card's own sentence on its button. A
  // decline or a limit is a choice to make (amber, no button: the same words
  // would be declined again, a limit needs its reset); an error, an exit, a
  // silence or a lost machine (red) offers Resume once the turn had started,
  // else the last prompt again (`lib/messageBlocks.sendAgainAction`).
  if (subtype === SYSTEM_SUBTYPE.TURN_ENDED) {
    const resend = !!reason && RESEND_REASONS.has(reason)
    const line = sendAgain ? withoutInstruction(message) : message
    return (
      <div className={`${NOTICE_BOX} ${resend ? NOTICE.danger : NOTICE.warning}`} data-testid="turn-ended" data-reason={reason}>
        <div className="font-medium">This turn ended early</div>
        {line && <div className="mt-1 whitespace-pre-wrap break-words">{line}</div>}
        {sendAgain && onSendAgain && (
          <>
            <div className="mt-1">
              {sendAgain === 'resume'
                ? 'Resume asks the agent to carry on from where it stopped.'
                : 'Send again sends your message again.'}
            </div>
            <button
              type="button"
              onClick={onSendAgain}
              className="mt-2 px-3 py-1 rounded-control border border-current text-xs font-medium hover:underline"
            >
              {sendAgain === 'resume' ? 'Resume' : 'Send again'}
            </button>
          </>
        )}
      </div>
    )
  }

  // Auto-continued with a fresh session: the chat's pinned
  // machine was deleted (or its session files aged out), so the proxy
  // spawned a new session seeded from DB history. Persisted — renders on
  // live push and on every reload at the discontinuity point.
  if (subtype === SYSTEM_SUBTYPE.SESSION_RESEEDED) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.info}`}>
        <div className="font-medium">Continued with a fresh session</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // A person moved the chat to another place (persisted): where it went and
  // what went with it, or, when the move did not finish, that the chat
  // stayed where it was. Neutral either way: a choice someone made.
  if (subtype === SYSTEM_SUBTYPE.CHAT_MOVED) {
    const failed = reason === CHAT_MOVED_REASON.FAILED
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.neutral}`} data-testid="chat-moved" data-reason={reason || undefined}>
        <div className="font-medium text-primary">{failed ? 'Chat move did not finish' : 'Chat moved'}</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // The chat's tools on its machine (persisted): still installing when the
  // session started, or how they ended. Neutral: nothing failed in the chat.
  if (subtype === SYSTEM_SUBTYPE.TOOLS_INSTALLING) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.neutral}`} data-testid="tools-installing">
        <div className="font-medium text-primary">Tools on this computer</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // No usable subscription for this execution layer (user-scoped warmup blocked).
  // A setup prompt, not a crash — amber, points the user at their settings.
  if (subtype === SYSTEM_SUBTYPE.NO_SUBSCRIPTION) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.warning}`}>
        <div className="font-medium">Subscription required</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // The pool's subscription cap refused the spawn (the proxy's wording says
  // which cap and where it is set). Amber: a limit the user or an admin
  // chose, not a crash.
  if (subtype === SYSTEM_SUBTYPE.POOL_CAP) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.warning}`}>
        <div className="font-medium">Subscription cap reached</div>
        {message && <div className="mt-1">{message}</div>}
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
      <div className={`${NOTICE_BOX} ${NOTICE.danger}`}>
        <div className="font-medium">Remote machine unavailable</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // A chat that runs as the agent, for a person below the editor role: a
  // role matter, not a crash; the message is the server's sentence.
  if (subtype === SYSTEM_SUBTYPE.BELOW_EDITOR) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.warning}`}>
        <div className="font-medium">This chat takes the editor role</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // Session failed to START on a reachable machine (config/spawn error — e.g.
  // a bad config.toml). NOT an availability problem, so a distinct title from
  // 'target_unavailable'; carries the backend's error for diagnosis.
  if (subtype === SYSTEM_SUBTYPE.SESSION_ERROR) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.danger}`}>
        <div className="font-medium">Couldn’t start the session</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // Meeting never started (admission denial, spawn failure) — red card with
  // the orchestrator's reason; the meeting pill is cleared by the handler.
  if (subtype === SYSTEM_SUBTYPE.MEETING_FAILED) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.danger}`}>
        <div className="font-medium">Meeting could not start</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // Meeting started: branded banner
  if (subtype === SYSTEM_SUBTYPE.MEETING_STARTED) {
    return (
      <div className="flex items-center justify-center gap-2 py-3 my-2 text-xs">
        <span className="h-px flex-1 bg-accent-agent-border" />
        <span className="text-accent-agent-fg font-medium px-2">Meeting started</span>
        <span className="h-px flex-1 bg-accent-agent-border" />
      </div>
    )
  }

  // Undelivered input: text the server could not deliver as a turn. A
  // message from the chat's queue (any reason: its delivery was refused,
  // the turn it waited for ended early, a Stop no tab of its author took)
  // was certainly not sent; a starting terminal's buffered text (no reason)
  // may have been swallowed by a TUI dialog. Full text shown un-truncated so
  // the user can copy it back.
  if (subtype === SYSTEM_SUBTYPE.UNDELIVERED_INPUT) {
    const queued = !!reason
    const why = reason === UNDELIVERED_REASON.TURN_FAILED
      ? 'It was queued behind a turn that ended early, so it was not sent.'
      : reason === UNDELIVERED_REASON.STOPPED
        ? 'It was queued when the turn was stopped, so it was not sent.'
        : 'It was queued and could not be sent as the next turn.'
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.warning}`}>
        <div className="font-medium">
          {queued ? 'This message was not sent' : 'This message may not have reached the agent'}
        </div>
        {message && (
          <div className="mt-1 whitespace-pre-wrap break-words select-text">{message}</div>
        )}
        <div className="mt-1 text-xs">
          {queued
            ? `${why} Copy it and send it again.`
            : 'It was typed while the session was starting. If the agent never answered it, copy it and send again.'}
        </div>
      </div>
    )
  }

  // A turn the server could not hold for the chat's machine (its row was
  // already written: a first prompt, an app interaction): live only.
  if (subtype === SYSTEM_SUBTYPE.MACHINE_RECONNECTING) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.warning}`} data-testid="machine-reconnecting">
        <div className="font-medium">The machine is reconnecting</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // The same turn when the machine is connected but did not answer the
  // resume probe in time: live only.
  if (subtype === SYSTEM_SUBTYPE.MACHINE_NOT_ANSWERING) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.warning}`} data-testid="machine-not-answering">
        <div className="font-medium">The machine is not answering</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // A permission request the engine raised that nobody answered (or that
  // its surface cannot answer): refused, and the turn went on.
  if (subtype === SYSTEM_SUBTYPE.ENGINE_PROMPT_REFUSED) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.warning}`} data-testid="engine-prompt-refused">
        <div className="font-medium">A permission request was refused</div>
        {message && <div className="mt-1">{message}</div>}
      </div>
    )
  }

  // A message typed while the agent worked (or the chat got ready) that the
  // server refused before any turn: live only, the text is back in the
  // composer.
  if (subtype === SYSTEM_SUBTYPE.QUEUE_REFUSED) {
    return (
      <div className={`${NOTICE_BOX} ${NOTICE.warning}`} data-testid="queue-refused">
        <div className="font-medium">This message was not queued</div>
        {message && <div className="mt-1">{message}</div>}
        <div className="mt-1 text-xs">
          {returned ? 'It is back in the composer.' : 'Type it again.'}
        </div>
      </div>
    )
  }

  // Context compressed: an information separator
  if (subtype === SYSTEM_SUBTYPE.CONTEXT_COMPRESSED) {
    return (
      <div className="flex items-center justify-center gap-2 py-3 my-2 text-xs">
        <span className="h-px flex-1 bg-status-info-border" />
        <span className="text-status-info-fg font-medium px-2">Context compressed</span>
        <span className="h-px flex-1 bg-status-info-border" />
      </div>
    )
  }

  // Meeting concluded: agent work's separator, like its start
  if (subtype === SYSTEM_SUBTYPE.MEETING_CONCLUDED) {
    return (
      <div className="flex items-center justify-center gap-2 py-3 my-2 text-xs">
        <span className="h-px flex-1 bg-accent-agent-border" />
        <span className="text-accent-agent-fg font-medium px-2">Meeting concluded</span>
        <span className="h-px flex-1 bg-accent-agent-border" />
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
    <div className="flex items-center justify-center gap-2 py-1 my-1 text-xs text-tertiary">
      <span className="h-px flex-1 bg-border" />
      <span>{label}</span>
      <span className="h-px flex-1 bg-border" />
    </div>
  )
}
