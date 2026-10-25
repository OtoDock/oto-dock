import { useState } from 'react'
import {
  useSetBrowserMode, useSetBrowserToken, useSetBrowserUnattended,
  type MachineScope, type RemoteMachine,
} from '../api/remoteMachines'
import { BROWSER_MODE, type BrowserMode, browserModeOf } from '../lib/remote/browserMode'
import { useDialogs } from './ui/dialogs'
import Button from './ui/Button'
import { Input, Select } from './ui/Field'
import InfoTip from './ui/InfoTip'
import { SaveState } from './ui/SettingsControls'
import { useSaveState } from '../hooks/useSaveState'

const EXTENSION_URL =
  'https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm'

// Own-browser mode for browser control, split in two pieces the grant UIs
// place themselves: the mode selector sits INSIDE the Browser-control row
// (visible while the grant is on), the token field sits under the grants
// while own mode is selected. Rendered by the admin page for admin-paired
// machines and by User Settings → My Machines for the caller's own. Same
// consent register as the grant: switching to own mode asks for
// confirmation. The extension token (what lets sessions connect without a
// click) is entered here and never shown again; a separate box lets
// unattended sessions (scheduled tasks, calls, meetings) use it too.
export function BrowserModeSelect({
  machine, scope,
}: { machine: RemoteMachine; scope: MachineScope }) {
  const dialogs = useDialogs()
  const setMode = useSetBrowserMode(scope)
  const mode = browserModeOf(machine)
  const { stateOf, run } = useSaveState()

  const change = async (next: BrowserMode) => {
    if (next === mode) return
    if (next === BROWSER_MODE.OWN && !(await dialogs.confirm({
      title: `Use your own browser on ${machine.name}?`,
      body:
      'Agents on this machine will work inside the browser you are signed into. ' +
      'They get their own tab group, and can reach your logins, cookies and open tabs. ' +
      'You can also let unattended runs use the saved token. ' +
      'Then scheduled tasks, triggers, calls and meetings use this browser with nobody watching. ' +
      'Those runs ask no one before a browser action.\n\n' +
      'Install the Playwright Extension in that browser first. Sessions already running ' +
      'switch when they next start.',
      confirmLabel: 'Use my browser',
    }))) return
    void run('mode', () => setMode.mutateAsync({ machineId: machine.id, mode: next }),
      { failTitle: 'The browser mode was not saved' })
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <Select compact
        aria-label="Browser mode"
        value={mode}
        disabled={setMode.isPending}
        onChange={e => change(e.target.value as BrowserMode)}
      >
        <option value="dedicated">Dedicated profile</option>
        <option value="own">My own browser</option>
      </Select>
      <SaveState state={stateOf('mode')} />
    </span>
  )
}

export function BrowserTokenField({
  machine, scope,
}: { machine: RemoteMachine; scope: MachineScope }) {
  const dialogs = useDialogs()
  const setToken = useSetBrowserToken(scope)
  const setUnattended = useSetBrowserUnattended(scope)
  const [draft, setDraft] = useState('')
  const { stateOf, run } = useSaveState()
  if (browserModeOf(machine) !== BROWSER_MODE.OWN) return null
  const tokenSet = machine.browser_extension_token_set === true

  const save = () => {
    const token = draft.trim()
    if (!token) return
    setToken.mutate({ machineId: machine.id, token }, { onSuccess: () => setDraft('') })
  }

  // Rendered INSIDE the Browser-control row's block (under its description,
  // above the next capability), separated by a hairline.
  return (
    <div className="mt-1.5 mb-1 ml-5 pt-1.5 border-t border-subtle max-w-md space-y-1.5">
      <p className="text-3xs leading-relaxed text-tertiary flex flex-wrap items-center gap-x-1.5 gap-y-1">
        <span>Install the Playwright Extension in your browser</span>
        <a
          href={EXTENSION_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-control border border-subtle text-primary hover:bg-surface-hover"
        >
          Get extension <span aria-hidden>↗</span>
        </a>
        <span>then copy the token from its page and paste it here.</span>
        <InfoTip label="Playwright Extension token">Saving it lets the sessions you drive connect without a click.</InfoTip>
      </p>
      <div className="flex items-center gap-2 flex-wrap">
        <Input compact
          type="password"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') save() }}
          placeholder={tokenSet ? 'Replace token…' : 'Paste the token here…'}
          autoComplete="off"
          aria-label="Playwright Extension token"
          className="flex-1 min-w-[12rem]"
        />
        <Button variant="secondary" size="xs"
          type="button"
          onClick={save}
          disabled={!draft.trim() || setToken.isPending}
        >
          {setToken.isPending ? 'Saving…' : 'Save token'}
        </Button>
        {tokenSet && (
          <span className="inline-flex items-center gap-1 text-3xs text-status-success-fg">
            <span className="h-1.5 w-1.5 rounded-full bg-status-success-solid" aria-hidden />
            Token saved
          </span>
        )}
        {tokenSet && (
          <Button variant="ghost" size="xs"
            onClick={() => setToken.mutate({ machineId: machine.id, token: null })}
            disabled={setToken.isPending}
          >
            Clear
          </Button>
        )}
      </div>
      {tokenSet && (
        <div className="flex items-start">
        <label className="flex items-start gap-1.5 text-3xs leading-relaxed text-tertiary">
          <input
            type="checkbox"
            checked={machine.browser_unattended === true}
            disabled={setUnattended.isPending}
            onChange={async e => {
              const enabled = e.target.checked
              if (enabled && !(await dialogs.confirm({
                title: `Let unattended runs use your browser on ${machine.name}?`,
                body: 'Scheduled tasks, triggers, calls and meetings will drive the browser you are ' +
                  'signed into. Nobody watches, and no one is asked before a browser action.',
                confirmLabel: 'Let them use it',
              }))) return
              void run('unattended', () => setUnattended.mutateAsync({ machineId: machine.id, enabled }),
                { failTitle: 'The setting was not saved' })
            }}
            className="mt-0.5 accent-action"
          />
          <span>
            Also let unattended runs like scheduled tasks, triggers, calls and meetings use
            this browser. Off, they cannot use it at all.
          </span>
        </label>
        <SaveState state={stateOf('unattended')} />
        </div>
      )}
      {setToken.error && (
        <p className="text-3xs text-status-danger-fg">{setToken.error.message}</p>
      )}
    </div>
  )
}
