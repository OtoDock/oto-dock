import { useState } from 'react'
import AlertDialog from '../ui/AlertDialog'

/** The provisional cross-engine pick shown in the banner (the engine's
 * display label resolved by the page from the layer catalog). */
export interface PendingEngineSwitch {
  layer: string
  layerLabel: string
  model: string
}

interface ConfirmProps {
  toLabel: string
  busy: boolean
  /** switch_engine_denied message — rendered inside the dialog (the generic
   * error rail only renders into an open stream bubble, invisible on the
   * idle dead chats this op runs on). */
  error: string | null
  onConfirm: () => void
  onCancel: () => void
}

/** Confirm dialog for the cross-engine switch: the restart-from-history
 * statement plus in-dialog busy/denial states. */
export function EngineSwitchConfirm({ toLabel, busy, error, onConfirm, onCancel }: ConfirmProps) {
  return (
    <AlertDialog
      title={`Switch this chat to ${toLabel}?`}
      confirmLabel="Switch engine"
      busy={busy}
      busyLabel="Switching…"
      error={error ? <span data-testid="engine-switch-error">{error}</span> : null}
      onConfirm={onConfirm}
      onCancel={onCancel}
    >
      This starts a fresh {toLabel} session and loads the chat's history.
    </AlertDialog>
  )
}

interface Props {
  chatId: string | null
  /** null → the banner unmounts (no provisional cross-engine pick). */
  pending: PendingEngineSwitch | null
  busy: boolean
  error: string | null
  /** Fires the switch_engine WS op (after the confirm). */
  onConfirm: () => void
  /** Reverts the provisional selection (the ✕). */
  onCancel: () => void
}

/** Bottom notice for a provisional cross-engine model pick on a dead chat:
 * prompting is blocked while it shows (the page disables send), and the
 * switch only happens through the button + confirm dialog. The ✕ reverts the
 * dropdown to the chat's current model. ChatTargetBanner is suppressed by
 * the page while this renders (two competing "restart from DB history"
 * offers would stack on a dead+mispinned chat). */
export default function EngineSwitchBanner({ chatId, pending, busy, error, onConfirm, onCancel }: Props) {
  const [confirming, setConfirming] = useState(false)
  // A new pick / chat switch resets the dialog (MoveChatConfirm convention:
  // the opener owns the confirm state and resets it on chat change).
  const pickKey = `${chatId ?? ''}|${pending?.layer ?? ''}|${pending?.model ?? ''}`
  const [forPick, setForPick] = useState(pickKey)
  if (forPick !== pickKey) {
    setForPick(pickKey)
    setConfirming(false)
  }

  if (!chatId || !pending) return null

  return (
    <>
      <div
        role="status"
        data-testid="engine-switch-banner"
        className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-card border border-status-info-border bg-status-info-bg text-status-info-fg"
      >
        <div className="flex items-center gap-2">
          <span className="min-w-0">
            Continue on <strong>{pending.layerLabel}</strong>? The chat restarts from its history.
          </span>
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="ml-auto shrink-0 px-2 py-1 rounded-control border border-current hover:underline"
          >
            Switch to {pending.layerLabel}
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="shrink-0 px-1.5 py-0.5 rounded-control hover:underline"
            title="Keep the current engine"
          >
            ×
          </button>
        </div>
      </div>
      {confirming && (
        <EngineSwitchConfirm
          toLabel={pending.layerLabel}
          busy={busy}
          error={error}
          onConfirm={onConfirm}
          onCancel={() => setConfirming(false)}
        />
      )}
    </>
  )
}
