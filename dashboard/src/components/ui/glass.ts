// Glass: a translucent fill under a blur. It belongs to the chat chrome
// only (the top bar's pills and buttons, the floating panel toggles, the
// Active now panel, the composer); everything else sits on a surface step.
// One recipe, so the bar's controls cannot drift apart.

/** The glass surface: fill, blur, edge and the card elevation. */
export const GLASS = 'bg-glass-fill backdrop-blur-xs border border-glass-border shadow-elevation-1'

/** The hover of a glass control. */
export const GLASS_HOVER = 'hover:bg-glass-hover'

/** A glass control while what it opens is open (the bell's panel, the
 *  avatar's menu): the hover's fill at rest, in place of `GLASS`. */
export const GLASS_PRESSED = 'bg-glass-hover backdrop-blur-xs border border-glass-border shadow-elevation-1'

/** The glass surface without its edge, for a control whose border carries a
 *  meaning (a plan's status, the artifact dock's action edge): with no class
 *  merging, `GLASS` and a second border colour would be two rules in the
 *  stylesheet's order. */
export const GLASS_FILL = 'bg-glass-fill backdrop-blur-xs shadow-elevation-1'
