import { useState } from 'react'

/**
 * Reusable type-the-slug confirmation modal for destructive actions.
 *
 * Used by:
 *   - Agent deletion (AgentConfig)
 *   - "Clear all memory" buttons (admin Platform page)
 *   - "Clear my memory" button (UserSettings)
 *   - Admin git revert action
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
}: {
  title: string
  description: React.ReactNode
  confirmWord: string
  confirmLabel?: string
  busyLabel?: string
  destructive?: boolean
  onConfirm: () => void
  onCancel: () => void
  isPending?: boolean
}) {
  const [typed, setTyped] = useState('')
  // Case-insensitive + trimmed match: the confirm word is a deliberate-friction
  // gate, not a password. Requiring exact case (e.g. "CONFIRM" but not "confirm")
  // silently leaves the submit button disabled and reads as "there is no button".
  const matches = typed.trim().toLowerCase() === confirmWord.trim().toLowerCase()
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl border border-p-border-light w-full max-w-sm mx-4 p-5">
        <h3 className={`text-base font-semibold mb-2 ${destructive ? 'text-red-600' : 'text-p-text'}`}>
          {title}
        </h3>
        <p className="text-sm text-p-text mb-3">
          {description}
          {' '}
          Type{' '}
          <span className={`font-mono font-bold ${destructive ? 'text-red-600' : 'text-p-text'}`}>
            {confirmWord}
          </span>
          {' '}to confirm.
        </p>
        <input
          autoFocus
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={confirmWord}
          className={`w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 ${destructive ? 'focus:ring-red-400' : 'focus:ring-blue-400'} mb-4`}
        />
        <div className="flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={!matches || isPending}
            className={`px-3 py-1.5 text-sm font-medium rounded-lg text-white transition-colors disabled:cursor-not-allowed disabled:bg-gray-200 disabled:text-gray-400 dark:disabled:bg-gray-700 dark:disabled:text-gray-500 ${destructive ? 'bg-red-600 hover:bg-red-700' : 'bg-blue-600 hover:bg-blue-700'}`}
          >
            {isPending ? (busyLabel ?? `${confirmLabel}...`) : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
