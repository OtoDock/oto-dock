/**
 * Where a phone's status badges float. Below `md` the badges (the work still
 * running, a live meeting) leave the status bar's own row, which pushed the
 * chat and the terminal up whenever one came or went, and sit over the
 * chat's bottom right corner instead: `ChatBottomPanel` holds the slot, just
 * above the panel so no banner is ever under a pill, and `ChatStatusBar`
 * renders the pills into it. The slot lays nothing out: only the pills take
 * a tap.
 */
import { createContext } from 'react'

/** The slot's element, null outside a bottom panel. */
export const BadgeFloatSlot = createContext<HTMLElement | null>(null)

/** The slot: at the panel's top right, above the chat (`z-10`, later in the
 *  page than the terminal's badge column and the scroll button) and below
 *  the document pane, the permission card, the artifact windows and every
 *  popover; never reaching the centred scroll-to-bottom button; a second row
 *  of pills wraps upward. */
export const BADGE_FLOAT_SLOT =
  'pointer-events-none absolute right-3 bottom-full mb-1.5 z-10 flex flex-wrap-reverse items-end justify-end gap-1 max-w-[calc(50%-2.25rem)] md:hidden'

/** What the pane shows, as far as the badges care. */
export interface BadgePane {
  workspaceOpen: boolean
  appsActive: boolean
  projectsActive: boolean
  /** A live terminal session, and whether the history covers it. */
  sessionInteractive: boolean
  showRichView: boolean
}

/** Where the badges go: the slot while the message list or the terminal
 *  holds the pane (over the workspace, an app or the projects they keep
 *  their row: null), and the compact form over the terminal, whose last
 *  line carries the engine's own text. */
export function badgePlacement(slot: HTMLElement | null, pane: BadgePane): { float: HTMLElement | null; compact: boolean } {
  const overlay = pane.workspaceOpen || pane.appsActive || pane.projectsActive
  return { float: overlay ? null : slot, compact: pane.sessionInteractive && !pane.showRichView }
}
