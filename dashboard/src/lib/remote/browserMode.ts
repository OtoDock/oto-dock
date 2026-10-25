// A machine's browser-control mode (the dedicated profile or the person's own
// browser) and the sentence the grant row shows for it.

import type { RemoteMachine } from '../../api/remoteMachines'

/** The browser-control modes (`browser_mode` on a machine). */
export const BROWSER_MODE = { DEDICATED: 'dedicated', OWN: 'own' } as const
export type BrowserMode = (typeof BROWSER_MODE)[keyof typeof BROWSER_MODE]

export const browserModeOf = (machine: RemoteMachine): BrowserMode =>
  machine.browser_mode === BROWSER_MODE.OWN ? BROWSER_MODE.OWN : BROWSER_MODE.DEDICATED

// The Browser-control row's description, by mode — the row reads as one
// sentence whichever mode is selected.
export const browserModeDesc = (mode: BrowserMode): string =>
  mode === BROWSER_MODE.OWN
    ? 'The agent works inside the Chrome, Edge or Brave you are signed into'
    : 'A separate browser profile for each agent on this computer'
