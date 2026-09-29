import { useState } from 'react'
import {
  useSetBrowserMode, useSetBrowserToken,
  type MachineScope, type RemoteMachine,
} from '../api/remoteMachines'

const EXTENSION_URL =
  'https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm'

export type BrowserMode = 'dedicated' | 'own'

export const browserModeOf = (machine: RemoteMachine): BrowserMode =>
  machine.browser_mode === 'own' ? 'own' : 'dedicated'

// The Browser-control row's description, by mode — the row reads as one
// sentence whichever mode is selected.
export const browserModeDesc = (mode: BrowserMode): string =>
  mode === 'own'
    ? 'the agent works inside the Chrome, Edge or Brave you are signed into'
    : 'a dedicated per-agent browser profile on this machine'

// Own-browser mode for browser control, split in two pieces the grant UIs
// place themselves: the mode selector sits INSIDE the Browser-control row
// (visible while the grant is on), the token field sits under the grants
// while own mode is selected. Rendered by the admin page for admin-paired
// machines and by User Settings → My Machines for the caller's own. Same
// consent register as the grant: switching to own mode asks for
// confirmation. The extension token (what lets sessions connect without a
// click — scheduled tasks, calls and meetings need it) is entered here and
// never shown again.
export function BrowserModeSelect({
  machine, scope,
}: { machine: RemoteMachine; scope: MachineScope }) {
  const setMode = useSetBrowserMode(scope)
  const mode = browserModeOf(machine)

  const change = (next: BrowserMode) => {
    if (next === mode) return
    if (next === 'own' && !window.confirm(
      `Use your own browser on ${machine.name}?\n\n` +
      'Agents on this machine will work inside the browser you are signed into, in ' +
      'their own tab group: your logins, cookies and open tabs are reachable to them. ' +
      'With the extension token saved, scheduled tasks, triggers, calls and meetings on this ' +
      'machine use this browser with nobody watching, and browser actions are not asked ' +
      'about; only the tools the browser MCP marks high-risk still need a person.\n\n' +
      'Install the Playwright Extension in that browser first. Sessions already running ' +
      'switch when they next start.',
    )) return
    setMode.mutate({ machineId: machine.id, mode: next })
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <select
        aria-label="Browser mode"
        value={mode}
        disabled={setMode.isPending}
        onChange={e => change(e.target.value as BrowserMode)}
        className="text-xs rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-p-text px-1.5 py-0.5"
      >
        <option value="dedicated">Dedicated profile</option>
        <option value="own">My own browser</option>
      </select>
      {setMode.error && (
        <span className="text-[10px] text-red-600">{setMode.error.message}</span>
      )}
    </span>
  )
}

export function BrowserTokenField({
  machine, scope,
}: { machine: RemoteMachine; scope: MachineScope }) {
  const setToken = useSetBrowserToken(scope)
  const [draft, setDraft] = useState('')
  if (browserModeOf(machine) !== 'own') return null
  const tokenSet = machine.browser_extension_token_set === true

  const save = () => {
    const token = draft.trim()
    if (!token) return
    setToken.mutate({ machineId: machine.id, token }, { onSuccess: () => setDraft('') })
  }

  // Rendered INSIDE the Browser-control row's block (under its description,
  // above the next capability), separated by a hairline.
  return (
    <div className="mt-1.5 mb-1 ml-5 pt-1.5 border-t border-p-border-light max-w-md space-y-1.5">
      <p className="text-[10px] leading-relaxed text-p-text-light flex flex-wrap items-center gap-x-1.5 gap-y-1">
        <span>Install the Playwright Extension in your browser</span>
        <a
          href={EXTENSION_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-sm border border-p-border-light text-p-text hover:bg-p-surface"
        >
          Get extension <span aria-hidden>↗</span>
        </a>
        <span>then copy the token from its page and paste it here.</span>
        <span>Saving it lets scheduled tasks, triggers, calls and meetings use this browser unattended.</span>
      </p>
      <div className="flex items-center gap-2 flex-wrap">
        <input
          type="password"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') save() }}
          placeholder={tokenSet ? 'Replace token…' : 'Paste the token here…'}
          autoComplete="off"
          aria-label="Playwright Extension token"
          className="flex-1 min-w-[12rem] px-2 py-1 text-xs rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-p-text"
        />
        <button
          type="button"
          onClick={save}
          disabled={!draft.trim() || setToken.isPending}
          className="px-2 py-1 text-xs font-medium rounded-sm border border-p-border-light text-p-text hover:bg-p-surface disabled:opacity-50"
        >
          {setToken.isPending ? 'Saving…' : 'Save token'}
        </button>
        {tokenSet && (
          <span className="inline-flex items-center gap-1 text-[10px] text-emerald-600">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
            Token saved
          </span>
        )}
        {tokenSet && (
          <button
            type="button"
            onClick={() => setToken.mutate({ machineId: machine.id, token: null })}
            disabled={setToken.isPending}
            className="px-2 py-1 text-xs rounded-sm text-p-text-light hover:text-p-text disabled:opacity-50"
          >
            Clear
          </button>
        )}
      </div>
      {setToken.error && (
        <p className="text-[10px] text-red-600">{setToken.error.message}</p>
      )}
    </div>
  )
}
