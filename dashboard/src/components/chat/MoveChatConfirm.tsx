import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { pushEscHandler } from '../../lib/escStack'

interface Props {
  /** Human label of the target the chat moves TO (the resolved_label). */
  label: string
  onConfirm: () => void
  onCancel: () => void
}

/** Confirm dialog for "Move this chat to <target>" — opened by the
 * ChatTargetBanner button and the sidebar kebab's move row. Mirrors
 * DeleteConfirmDialog's portal shape; the wording is the honest
 * fork-from-history statement: the move starts a FRESH session and the old
 * one is abandoned. */
export default function MoveChatConfirm({ label, onConfirm, onCancel }: Props) {
  useEffect(() => pushEscHandler(onCancel), [onCancel])

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs"
      onClick={(e) => { e.stopPropagation(); onCancel() }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[min(400px,90vw)] rounded-xl bg-white dark:bg-p-surface border border-p-border-light shadow-xl p-4"
      >
        <h3 className="text-sm font-semibold text-p-text mb-2">Move chat to {label}?</h3>
        <p className="text-sm text-p-text-secondary mb-4">
          This starts a fresh session on {label} and loads this chat's history
          from the database. The old session stays behind.
        </p>
        <div className="flex items-center justify-end gap-2">
          <button
            onClick={(e) => { e.stopPropagation(); onCancel() }}
            className="px-3 py-1.5 text-xs rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-p-text hover:bg-p-surface-hover"
          >
            Cancel
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onConfirm() }}
            className="px-3 py-1.5 text-xs rounded-lg bg-brand text-white hover:bg-brand-hover"
          >
            Move chat
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
