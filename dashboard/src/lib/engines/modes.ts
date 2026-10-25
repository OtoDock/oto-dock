/**
 * The permission modes, from the engine's descriptor (`permission_modes` —
 * the proxy's `PERMISSION_MODES` subset the engine declares; the WS mode
 * change is validated against the same list). The labels and glyphs live
 * with the status bar; this answers which modes a chat may offer and what an
 * old stored word reads as. A meeting never plans (the platform's rule, not
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

/** The words a release before 1.9 stored (a chat row, this browser's
 *  remembered pick) and what they read as now. */
const MODES_BEFORE_1_9: Readonly<Record<string, string>> = { default: ASK_MODE, acceptEdits: AUTO_REVIEW_MODE }

/** A stored or remembered mode in the current vocabulary. */
export function currentMode(mode: string): string {
  return MODES_BEFORE_1_9[mode] ?? mode
}

/** The mode a new chat starts in: the person's last pick for the agent (an
 *  old word read as its current one), else Auto. */
export function seedMode(lastPick: string | undefined): string {
  return lastPick ? currentMode(lastPick) : AUTO_REVIEW_MODE
}

/** A chat that runs every tool without asking: Don't Ask, and a task's
 *  stored `auto` (shown as Don't Ask). */
const WITHOUT_ASKING: ReadonlySet<string> = new Set([DONT_ASK_MODE, UNATTENDED_MODE])

export function runsWithoutAsking(mode: string | undefined): boolean {
  return !!mode && WITHOUT_ASKING.has(mode)
}

/** The Don't Ask chip's tint, border and glyph colour: the status bar's
 *  mode icon and the terminal's badge read the same three classes. */
export const DONT_ASK_CHIP = {
  bg: 'bg-status-warning-bg',
  border: 'border-status-warning-border',
  text: 'text-status-warning-fg',
} as const

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
