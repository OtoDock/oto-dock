import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { pushEscHandler } from '../../lib/escStack'

/** The provisional cross-engine pick shown in the banner (display labels
 * resolved by the page from the layer catalog). */
export interface PendingEngineSwitch {
  layer: string
  layerLabel: string
  model: string
  modelLabel: string
}

interface ConfirmProps {
  fromLabel: string
  toLabel: string
  busy: boolean
  /** switch_engine_denied message — rendered inside the dialog (the generic
   * error rail only renders into an open stream bubble, invisible on the
   * idle dead chats this op runs on). */
  error: string | null
  onConfirm: () => void
  onCancel: () => void
}

/** Confirm dialog for the cross-engine switch — MoveChatConfirm's portal
 * shape (esc-stack, backdrop cancels) with the honest restart-from-history
 * statement plus in-dialog busy/denial states. */
export function EngineSwitchConfirm({ fromLabel, toLabel, busy, error, onConfirm, onCancel }: ConfirmProps) {
  useEffect(() => pushEscHandler(onCancel), [onCancel])

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs"
      onClick={(e) => { e.stopPropagation(); if (!busy) onCancel() }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[min(400px,90vw)] rounded-xl bg-white dark:bg-p-surface border border-p-border-light shadow-xl p-4"
      >
        <h3 className="text-sm font-semibold text-p-text mb-2">Switch this chat to {toLabel}?</h3>
        <p className="text-sm text-p-text-secondary mb-2">
          This starts a fresh {toLabel} session and loads this chat's history
          from the database. The original {fromLabel} session file is left
          behind and won't be used again.
        </p>
        {error && (
          <p data-testid="engine-switch-error" className="text-xs text-p-error mb-2">{error}</p>
        )}
        <div className="flex items-center justify-end gap-2">
          <button
            onClick={(e) => { e.stopPropagation(); onCancel() }}
            disabled={busy}
            className="px-3 py-1.5 text-xs rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-p-text hover:bg-p-surface-hover disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onConfirm() }}
            disabled={busy}
            className="px-3 py-1.5 text-xs rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-50"
          >
            {busy ? 'Switching…' : 'Switch engine'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

interface Props {
  chatId: string | null
  /** null → the banner unmounts (no provisional cross-engine pick). */
  pending: PendingEngineSwitch | null
  /** Display label of the chat's CURRENT engine. */
  fromLabel: string
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
export default function EngineSwitchBanner({ chatId, pending, fromLabel, busy, error, onConfirm, onCancel }: Props) {
  const [confirming, setConfirming] = useState(false)
  // A new pick / chat switch resets the dialog (MoveChatConfirm convention:
  // the opener owns the confirm state and resets it on chat change).
  useEffect(() => { setConfirming(false) }, [chatId, pending?.layer, pending?.model])

  if (!chatId || !pending) return null

  return (
    <>
      <div
        role="status"
        data-testid="engine-switch-banner"
        className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-sm border border-brand/40 bg-brand-50/60 text-p-text dark:bg-brand/10"
      >
        <div className="flex items-center gap-2">
          <span className="min-w-0">
            Continue on <strong>{pending.layerLabel}</strong> ({pending.modelLabel})?
            This chat will be restarted from database history — not from its
            original {fromLabel} session file.
          </span>
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="ml-auto shrink-0 px-2 py-1 rounded-sm border border-brand/40 hover:bg-brand/15"
          >
            Switch to {pending.layerLabel}
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="shrink-0 px-1.5 py-0.5 rounded-sm hover:bg-brand/15"
            title="Keep the current engine"
          >
            ×
          </button>
        </div>
      </div>
      {confirming && (
        <EngineSwitchConfirm
          fromLabel={fromLabel}
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
