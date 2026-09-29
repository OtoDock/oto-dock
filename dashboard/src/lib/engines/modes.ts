/**
 * The permission-mode menu, from the engine's descriptor
 * (`permission_modes` — the proxy's `PERMISSION_MODES` subset the engine
 * declares; the WS mode change is validated against the same list). The
 * labels and glyphs live with the status bar; this answers which modes a
 * chat may offer. A meeting never plans (the platform's rule, not an
 * engine's). `auto` (a task's silent mode) and `judge` (a check's read-only
 * profile) are stored on chats but never offered.
 */
import type { EngineDescriptor } from '../../api/engineDescriptor'

export const PLAN_MODE = 'plan'

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
