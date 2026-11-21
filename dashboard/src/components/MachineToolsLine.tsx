/**
 * What a machine's card says about its tools while install ahead works: the
 * tools it is installing now, and each tool that could not be installed with
 * its cause behind the info tip. Nothing once everything is installed. The
 * state comes on the machine's row (`tools_ahead`); a card reads the rows
 * more often while one says installing (`toolsAheadRefetch`).
 */

import InfoTip from './ui/InfoTip'
import type { RemoteMachine } from '../api/remoteMachines'
import { machineToolsSentence } from '../lib/status/machineTools'

export default function MachineToolsLine({ machine }: { machine: RemoteMachine }) {
  const tools = machine.tools_ahead
  if (!tools) return null
  const failed = Object.entries(tools.failed ?? {})
  return (
    <div className="mt-1 space-y-0.5 text-xs" data-testid="machine-tools-line">
      {tools.installing.length > 0 && (
        <p className="text-secondary">{machineToolsSentence(tools.installing)}</p>
      )}
      {failed.map(([label, cause]) => (
        <p key={label} className="flex items-center gap-1 text-status-warning-fg">
          {label} could not be installed. Chats there may start without it.
          <InfoTip label={`Why ${label} could not be installed`}>{cause}</InfoTip>
        </p>
      ))}
    </div>
  )
}
