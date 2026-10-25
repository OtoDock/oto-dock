import { useId, useState } from 'react'

import Dialog from './ui/Dialog'
import Button from './ui/Button'
import { DIALOG_PANEL } from './ui/classes'
import { Input } from './ui/Field'

/**
 * Reusable type-the-slug confirmation modal for destructive actions.
 *
 * Used by:
 *   - Deleting an agent, stopping sharing and switching its visibility mode (AgentConfig)
 *   - "Clear the shared agent memory" (AgentConfig.memory)
 *   - "Clear my memory" (UserSettings)
 *
 * Escape closes it; a click on the backdrop does not (what was typed would
 * go), and nothing closes it while the action runs.
 */
export default function StrongConfirmModal({
  title,
  description,
  confirmWord,
  confirmLabel = 'Confirm',
  busyLabel,
  destructive = true,
  onConfirm,
  onCancel,
  isPending = false,
  confirmDisabled = false,
  extra,
}: {
  title: string
  description: React.ReactNode
  /** Block content shown under the description (a list, an error). */
  extra?: React.ReactNode
  confirmWord: string
  confirmLabel?: string
  busyLabel?: string
  destructive?: boolean
  onConfirm: () => void
  onCancel: () => void
  isPending?: boolean
  /** Keeps the confirm button off while the dialog is not ready to confirm. */
  confirmDisabled?: boolean
}) {
  const [typed, setTyped] = useState('')
  const id = useId()
  // Case-insensitive + trimmed match: the confirm word is a deliberate-friction
  // gate, not a password. Requiring exact case (e.g. "CONFIRM" but not "confirm")
  // silently leaves the submit button disabled and reads as "there is no button".
  const matches = typed.trim().toLowerCase() === confirmWord.trim().toLowerCase()
  return (
    <Dialog
      onClose={onCancel}
      dismissible="escape"
      busy={isPending}
      role="alertdialog"
      aria-labelledby={`${id}-title`}
      aria-describedby={`${id}-body`}
      panelClassName={`${DIALOG_PANEL} max-w-sm p-5`}
    >
        <h3 id={`${id}-title`} className={`text-base font-semibold mb-2 ${destructive ? 'text-status-danger-fg' : 'text-primary'}`}>
          {title}
        </h3>
        <p id={`${id}-body`} className="text-sm text-primary mb-3">
          {description}
          {' '}
          Type{' '}
          <span className={`font-mono font-bold ${destructive ? 'text-status-danger-fg' : 'text-primary'}`}>
            {confirmWord}
          </span>
          {' '}to confirm.
        </p>
        {extra && <div className="mb-3">{extra}</div>}
        <Input
          autoFocus
          aria-label={`Type ${confirmWord} to confirm`}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={confirmWord}
          className="mb-4"
        />
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onCancel} disabled={isPending}>
            Cancel
          </Button>
          <Button
            variant={destructive ? 'danger' : 'primary'}
            onClick={onConfirm}
            disabled={!matches || isPending || confirmDisabled}
          >
            {isPending ? (busyLabel ?? `${confirmLabel}...`) : confirmLabel}
          </Button>
        </div>
    </Dialog>
  )
}
