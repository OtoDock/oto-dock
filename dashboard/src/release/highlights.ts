// The release dialog's content: a curated file bundled with the dashboard
// (highlights.json, one entry per release, written at the freeze), rendered
// as cards with a fixed icon set. The server decides who is eligible to be
// told (GET /v1/users/me/release-card); this file decides what they see, and
// whether a member sees anything at all (an empty `everyone` list: nothing).
// tests/releaseHighlights.test.ts and the release gate hold the file to the
// rules below.

import data from './highlights.json'

export type Audience = 'everyone' | 'admins'

export interface Highlight {
  icon: string
  title: string
  text: string
}

export type ReleaseHighlights = Record<Audience, Highlight[]>

export const HIGHLIGHT_LIMITS = { title: 40, text: 160, cards: 6 } as const
export const VERSION_RE = /^\d+\.\d+\.\d+$/

// The dashboard has no icon library: a dozen outline shapes, one path each
// (24 x 24, stroked). A card names one by its key.
export const ICON_PATHS = {
  chat: 'M8 10h8M8 14h5M21 12c0 4.4-4 8-9 8a9.7 9.7 0 0 1-4-.9L3 20l1.3-3.6A7.5 7.5 0 0 1 3 12c0-4.4 4-8 9-8s9 3.6 9 8z',
  picture: 'M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zM3 17l5-5 4 4 3-3 6 6M15.5 9.5h.01',
  phone: 'M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z',
  mobile: 'M8 3h8a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM11 18h2',
  shield: 'M12 3l8 3v6c0 5-3.5 8.5-8 9-4.5-.5-8-4-8-9V6l8-3zM9 12l2 2 4-4',
  backup: 'M4 7h16v13H4zM3 4h18v3H3zM10 11h4',
  machine: 'M4 5h16a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zM8 20h8M12 16v4',
  calendar: 'M4 6h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1zM3 10h18M8 3v5M16 3v5',
  app: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
  document: 'M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM14 3v5h5M9 13h6M9 17h6',
  bell: 'M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20a2 2 0 0 0 4 0',
  key: 'M15 9a1 1 0 1 0 0-2 1 1 0 0 0 0 2zM14 3a7 7 0 0 0-6.6 9.3L3 16.8V21h4.2l4.5-4.5A7 7 0 1 0 14 3z',
  sparkle: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16z',
} as const

export type IconName = keyof typeof ICON_PATHS
export const ICON_NAMES = Object.keys(ICON_PATHS) as IconName[]

const highlights = data as Record<string, Partial<ReleaseHighlights>>

/** The cards of one release, both audiences (a release candidate reads its release's entry);
 *  empty lists for a release without an entry. */
export function highlightsFor(version: string): ReleaseHighlights {
  const entry = highlights[version.replace(/-rc\d+$/, '')]
  return { everyone: entry?.everyone ?? [], admins: entry?.admins ?? [] }
}
