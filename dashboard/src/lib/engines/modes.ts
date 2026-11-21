/**
 * The permission modes, from the engine's descriptor (`permission_modes` —
 * the proxy's `PERMISSION_MODES` subset the engine declares; the WS mode
 * change is validated against the same list). The labels and glyphs live
 * with the status bar; this answers which modes a chat may offer. A meeting never plans (the platform's rule, not
 * an engine's). `auto` (a task's silent mode, shown as Don't Ask) and `judge`
 * (a check's read-only profile) are stored on chats but never offered.
 */
import type { EngineDescriptor } from '../../api/engineDescriptor'

export const ASK_MODE = 'ask'
export const AUTO_REVIEW_MODE = 'autoReview'
export const DONT_ASK_MODE = 'dontAsk'
export const PLAN_MODE = 'plan'
/** A task's, trigger's or phone call's stored mode (shown as Don't Ask). */
export const UNATTENDED_MODE = 'auto'

/** The menu, in order (the proxy's `PERMISSION_MODES`). */
export const PERMISSION_MODES: readonly string[] = [ASK_MODE, AUTO_REVIEW_MODE, DONT_ASK_MODE, PLAN_MODE]

/** The mode a new chat starts in: the person's last pick for the agent,
 *  else Auto. */
export function seedMode(lastPick: string | undefined): string {
  return lastPick || AUTO_REVIEW_MODE
}

/** A chat that runs every tool without asking: Don't Ask, and a task's
 *  stored `auto` (shown as Don't Ask). */
const WITHOUT_ASKING: ReadonlySet<string> = new Set([DONT_ASK_MODE, UNATTENDED_MODE])

export function runsWithoutAsking(mode: string | undefined): boolean {
  return !!mode && WITHOUT_ASKING.has(mode)
}

/** The Don't Ask chip's tint, border and glyph colour (the status bar's
 *  mode icon). */
export const DONT_ASK_CHIP = {
  bg: 'bg-status-warning-bg',
  border: 'border-status-warning-border',
  text: 'text-status-warning-fg',
} as const

/** What the mode button reads to know whether a pick may go now. */
export interface ModeBusyState {
  /** A live terminal holds the chat. */
  terminalLive: boolean
  /** The chat runs as a terminal (its toggle on), live or not yet. */
  terminalChat: boolean
  /** A switch or a mode pick's restart runs. */
  switching: boolean
  /** The chat's session is starting. */
  starting: boolean
  /** A turn runs. */
  turnRuns: boolean
}

/** Why the mode button takes no pick now, or '': on a live terminal a pick
 *  starts it again, so not while a turn runs there (the machine button's
 *  rule and words), nor while the terminal starts or starts again (its start
 *  has read the mode already). A headless chat's pick reaches its running
 *  turn, so a turn never makes it busy. */
export function modeBusyReason(s: ModeBusyState): string {
  if (s.switching) return 'The chat is starting again.'
  if (s.terminalChat && s.starting) return 'The terminal is starting. Pick the mode again when it is up.'
  if (s.terminalLive && s.turnRuns) return 'Finish or stop the current turn before changing the mode.'
  return ''
}

/** The modes a chat on this engine may pick; every mode when the descriptor
 *  is unknown (the catalog has not loaded) so the menu is never empty. */
export function permissionModes(d: EngineDescriptor | undefined, fallback: string[]): string[] {
  const declared = d?.permission_modes
  return declared && declared.length > 0 ? declared : fallback
}

export function modeOffered(modes: string[], mode: string, opts: { meeting?: boolean } = {}): boolean {
  if (opts.meeting && mode === PLAN_MODE) return false
  return modes.includes(mode)
}

/** A plan review's implement answers: start the plan in Auto, or in Ask. */
export const IMPLEMENT_AUTO = 'implement_auto'
export const IMPLEMENT_ASK = 'implement_ask'
/** The answer names before 1.9 (a stored card's), read as the current ones. */
export const PLAN_ACTIONS_BEFORE_1_9: Readonly<Record<string, string>> = {
  implement_accept_edits: IMPLEMENT_AUTO,
  implement_default: IMPLEMENT_ASK,
}
