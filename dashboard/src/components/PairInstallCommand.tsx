import { useState } from 'react'
import { BUTTON_BASE, BUTTON_SIZE, SEGMENT } from './ui/classes'
import { type PairResult } from '../api/remoteMachines'
import { BOOTSTRAP_OS, detectBootstrapOs, type BootstrapOs } from '../lib/hostOs/os'

const OS_LABEL: Record<BootstrapOs, string> = {
  linux: 'Linux',
  macos: 'macOS',
  windows: 'Windows',
}

const OS_HELPER: Record<BootstrapOs, string> = {
  linux: 'Run in a terminal on the remote machine.',
  macos: 'Run in Terminal.app on the remote machine.',
  windows: 'Run in a normal PowerShell on the remote machine. No administrator rights are needed. Windows may ask once to install missing tools.',
}

interface Props {
  pairResult: PairResult
  /** Optional: copy shown above the OS tabs. Defaults to a generic message. */
  introText?: string
}

/**
 * Displays the install command from a pairing result, with OS tabs to
 * switch between Linux / macOS / Windows variants. Shared by both the
 * admin RemoteMachinesPage and the user UserSettings pair modals.
 */
export default function PairInstallCommand({ pairResult, introText }: Props) {
  const [os, setOs] = useState<BootstrapOs>(detectBootstrapOs())
  const cmd = pairResult.install_commands[os]

  return (
    <div className="space-y-3">
      {introText && (
        <p className="text-sm text-tertiary">{introText}</p>
      )}
      <div role="group" aria-label="Operating system" className="flex gap-1">
        {BOOTSTRAP_OS.map(o => (
          <button
            key={o}
            type="button"
            onClick={() => setOs(o)}
            aria-pressed={o === os}
            className={`${BUTTON_BASE} ${BUTTON_SIZE.xs} ${o === os ? SEGMENT.on : SEGMENT.off}`}
          >
            {OS_LABEL[o]}
          </button>
        ))}
      </div>
      <div className="bg-code-bg text-code-fg p-3 rounded-card text-xs font-mono break-all select-all">
        {cmd}
      </div>
      <p className="text-xs text-tertiary">{OS_HELPER[os]}</p>
    </div>
  )
}
