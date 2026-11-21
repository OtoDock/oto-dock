import { useState } from 'react'

import { useAddComputeMachine, type ComputeCommand } from '../../api/remoteMachines'
import { CopyButton } from '../../components/CopyButton'
import Button from '../../components/ui/Button'
import Dialog from '../../components/ui/Dialog'
import { Input, Label } from '../../components/ui/Field'
import { errorText } from '../../lib/http/apiJson'

const WARNING = 'rounded-card border border-status-warning-border bg-status-warning-bg px-3 py-2 text-sm text-status-warning-fg'

/** Where the server gets the platform's image, in one sentence. */
function imageSentence(command: Pick<ComputeCommand, 'image' | 'image_public'>): string {
  return command.image_public
    ? "The server downloads OtoDock's image, about 4 GB."
    : `First load the image ${command.image} on the server. This install was built from source, so the server cannot download it.`
}

/**
 * The one command that pairs a compute machine on another server: shown
 * after it is added and after "Pair again". It holds a pairing code that
 * works once, for an hour, and no secret: the server makes its own.
 */
export function ComputeCommandStep({ command }: { command: ComputeCommand }) {
  return (
    <div className="space-y-3 text-sm text-secondary" data-testid="compute-command">
      <p>Run this command on the server.</p>
      <div className="bg-code-bg text-code-fg p-3 rounded-card text-xs font-mono break-all select-all">
        {command.install_command}
      </div>
      <CopyButton text={command.install_command} />
      <p className="text-xs text-tertiary">
        Paste it on the server as an account that can use sudo. It works once, within {command.expires_in_hours === 1 ? 'one hour' : `${command.expires_in_hours} hours`}.
      </p>
      <p className="text-xs text-tertiary">{imageSentence(command)}</p>
      {command.plain && (
        <p className="text-xs text-status-warning-fg">This install has no https, so run it only on a network you trust.</p>
      )}
      <p>It shows as Online here once it signs in.</p>
    </div>
  )
}

/**
 * Adding a compute machine on another server: an admin's own act. The
 * warning comes first: every chat, terminal and task that runs there runs
 * with the accounts of the people who use it. Only a name is asked. Every
 * admin is told when it is added (the server sends the notice).
 */
export default function AddComputeMachineDialog({ onClose }: { onClose: () => void }) {
  const add = useAddComputeMachine()
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [command, setCommand] = useState<ComputeCommand | null>(null)

  const submit = () => {
    setError('')
    add.mutate({ name: name.trim() }, {
      onSuccess: setCommand,
      onError: e => setError(errorText(e) || 'The machine was not added.'),
    })
  }

  return (
    <Dialog
      onClose={onClose}
      busy={add.isPending}
      dismissible="escape"
      title={command ? `Set up ${command.name}` : 'Add a compute machine'}
      placement="top"
      data-testid="add-compute-machine"
    >
      {command ? (
        <div className="space-y-4 p-4">
          <ComputeCommandStep command={command} />
          <div className="flex justify-end">
            <Button variant="primary" onClick={onClose}>Done</Button>
          </div>
        </div>
      ) : (
        <form
          className="space-y-4 p-4"
          onSubmit={e => { e.preventDefault(); submit() }}
        >
          <div className={WARNING} data-testid="compute-add-warning">
            <p className="font-medium">Chats, terminals and tasks will run on this server.</p>
            <p className="mt-1">They run there with the accounts of the people who use them. Add only a server you control.</p>
          </div>
          <p className="text-sm text-secondary">If this server is lost, its chats go on from their history on another machine.</p>
          <div>
            <Label htmlFor="compute-name">Name</Label>
            <Input id="compute-name" value={name} onChange={e => setName(e.target.value)} autoFocus />
          </div>
          {error && <p className="text-sm text-status-danger-fg" role="alert">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button onClick={onClose} disabled={add.isPending}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={add.isPending || !name.trim()}>
              {add.isPending ? 'Adding…' : 'Add machine'}
            </Button>
          </div>
        </form>
      )}
    </Dialog>
  )
}
