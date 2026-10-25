// The primitives' class strings, on the semantic tokens (src/tokens.css).
// The components in this folder read them, and the token generator reads
// this file as text to draw the reference page, so each string is the one
// look of its primitive. A feature that builds its own button or card
// copies these strings, never a palette colour.

export const BUTTON_BASE =
  'inline-flex items-center justify-center gap-1.5 font-medium transition-colors duration-fast ' +
  'disabled:cursor-not-allowed disabled:opacity-50 ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring ' +
  'focus-visible:ring-offset-1 focus-visible:ring-offset-surface-page'

export const BUTTON_VARIANT = {
  primary: 'bg-action text-action-fg hover:bg-action-hover active:bg-action-active disabled:hover:bg-action',
  // A soft fill on the one surface that differs from both the page and the
  // card in both themes. The hover and the press lay the translucent hover
  // over the fill as a gradient layer (transparent at rest, so the colour
  // transition fades it both ways): darker in light, lighter in dark.
  // `bg-surface-hover` alone would replace the fill and turn lighter than it
  // in light.
  secondary:
    'border border-default bg-surface-muted text-primary bg-linear-to-b from-transparent to-transparent ' +
    'hover:from-surface-hover hover:to-surface-hover active:from-surface-hover active:to-surface-hover ' +
    'disabled:hover:from-transparent disabled:hover:to-transparent',
  danger: 'bg-status-danger-fill text-on-solid hover:bg-status-danger-fill-hover disabled:hover:bg-status-danger-fill',
  ghost: 'text-secondary hover:bg-surface-hover hover:text-primary',
} as const

// Heights from the control tokens (32, 36, 44 px); `min-h` so a label that
// wraps grows the button instead of spilling out of it.
export const BUTTON_SIZE = {
  xs: 'min-h-7 px-2.5 py-0.5 text-xs rounded-control',
  sm: 'min-h-control-sm px-3 py-1 text-sm rounded-control',
  md: 'min-h-control-md px-4 py-1.5 text-sm rounded-control',
  lg: 'min-h-control-lg px-5 py-2 text-base rounded-control',
} as const

// A field's boundary is border-strong (3:1 on every surface); invalid is
// the danger solid, which reads at 3:1 too.
export const FIELD =
  'w-full min-w-0 border bg-surface-card bg-clip-padding text-primary placeholder:text-tertiary ' +
  'focus:border-action focus:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring/40 ' +
  'disabled:cursor-not-allowed disabled:opacity-50'
export const FIELD_REGULAR = 'min-h-control-sm px-3 py-1.5 text-sm rounded-control'
export const FIELD_COMPACT = 'px-2 py-1 text-xs rounded-control'
export const FIELD_BORDER = 'border-strong'
export const FIELD_INVALID = 'border-status-danger-solid'

export const LABEL = 'mb-1 block text-xs font-medium text-primary'

export const SWITCH_TRACK =
  'relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent ' +
  'transition-colors duration-base ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring ' +
  'focus-visible:ring-offset-1 focus-visible:ring-offset-surface-page ' +
  'disabled:cursor-not-allowed disabled:opacity-40'
export const SWITCH_ON = 'bg-action'
export const SWITCH_OFF = 'bg-control-track'
export const SWITCH_THUMB =
  'pointer-events-none inline-block h-4 w-4 transform rounded-full bg-action-fg shadow-elevation-1 ring-0 ' +
  'transition duration-base'

// A badge names one meaning: status pills are tints with their own
// foreground, never a saturated fill under text.
export const BADGE_BASE = 'inline-flex items-center gap-1 rounded-chip px-2 py-0.5 text-xs font-medium whitespace-nowrap'
export const BADGE_TONE = {
  neutral: 'bg-surface-muted text-secondary',
  info: 'bg-status-info-bg text-status-info-fg',
  success: 'bg-status-success-bg text-status-success-fg',
  warning: 'bg-status-warning-bg text-status-warning-fg',
  danger: 'bg-status-danger-bg text-status-danger-fg',
  agent: 'bg-accent-agent-bg text-accent-agent-fg',
  media: 'bg-accent-media-bg text-accent-media-fg',
  action: 'bg-action-subtle text-link',
} as const

// A settings page's own title. Under 768 px the settings shell's header
// already names the section, so the page's title hides there; the header
// row's actions keep their place (`justify-end md:justify-between`).
export const PAGE_TITLE = 'hidden md:block font-display text-xl font-semibold text-primary'

// A row's Delete or Revoke is the quiet danger action; a red fill belongs to
// a confirm or an edit form.
export const BUTTON_DANGER_QUIET = `${BUTTON_BASE} ${BUTTON_SIZE.xs} text-status-danger-fg hover:bg-status-danger-bg`

// Depth from the surface step and the elevation. A group (a collapsible
// card) is CARD; an item inside it sits on the page's own colour with a
// hairline, a recess rather than a second raise.
export const CARD = 'rounded-card bg-surface-card shadow-elevation-1'
export const CARD_BORDERED = 'rounded-card border border-subtle bg-surface-page'

// A segmented control: secondary buttons in a row, the chosen one on the
// action fill, each with aria-pressed (BUTTON_BASE and a BUTTON_SIZE beside
// it). The chosen one keeps a 1 px edge in its own colour, so the labels do
// not move when the choice does. A forced-colour mode drops the fill, so the
// chosen one takes the system's selection colours there.
export const SEGMENT = {
  off: BUTTON_VARIANT.secondary,
  on:
    'border border-action bg-action text-action-fg forced-colors:forced-color-adjust-none ' +
    'forced-colors:border-[Highlight] forced-colors:bg-[Highlight] forced-colors:text-[HighlightText]',
} as const

// The dialog's panel without its width (the caller adds max-w-*).
export const DIALOG_PANEL = 'relative w-full rounded-dialog bg-surface-overlay text-primary shadow-elevation-4'
export const POPOVER = 'rounded-popover bg-surface-overlay text-primary shadow-elevation-3'
export const MENU_ITEM =
  'flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-secondary ' +
  'transition-colors duration-fast hover:bg-surface-hover hover:text-primary'
export const TOOLTIP = 'rounded-md bg-surface-inverse px-2.5 py-1.5 text-xs leading-snug text-inverse shadow-elevation-3'
// The info button beside a setting's label: a 16 px glyph in a 32 px box
// whose negative margins keep the label's line height, tertiary at rest and
// the link colour on hover and while its bubble shows (POPOVER, p-3 text-sm).
// On a coarse pointer an invisible layer grows the hit area to 44 px.
export const INFO_TIP_BUTTON =
  'relative inline-flex size-8 -my-2 -mr-2 shrink-0 items-center justify-center rounded-control align-middle ' +
  'pointer-coarse:before:absolute pointer-coarse:before:-inset-1.5 ' +
  'text-tertiary transition-colors duration-fast hover:text-link aria-expanded:text-link ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring'
export const TOAST = 'rounded-popover bg-surface-overlay text-primary shadow-elevation-3'
