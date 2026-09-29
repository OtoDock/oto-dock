import { useState } from 'react'
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
  windows: 'Run in a normal PowerShell on the remote machine — no administrator rights needed (Windows may prompt once to install missing tools).',
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
        <p className="text-sm text-p-text-light">{introText}</p>
      )}
      <div className="inline-flex rounded-lg border border-p-border-light p-0.5 bg-p-surface">
        {BOOTSTRAP_OS.map(o => (
          <button
            key={o}
            type="button"
            onClick={() => setOs(o)}
            className={
              'px-3 py-1 text-xs font-medium rounded-md transition-colors ' +
              (o === os
                ? 'bg-brand text-white'
                : 'text-p-text hover:bg-p-surface-hover')
            }
          >
            {OS_LABEL[o]}
          </button>
        ))}
      </div>
      <div className="bg-gray-900 text-green-400 p-3 rounded-lg text-xs font-mono break-all select-all">
        {cmd}
      </div>
      <p className="text-xs text-p-text-light">{OS_HELPER[os]}</p>
    </div>
  )
}
