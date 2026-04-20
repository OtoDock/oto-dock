import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { pushEscHandler } from '../../lib/escStack'

interface Props {
  /** All targets — usually one, multiple for batch delete. */
  names: string[]
  /** True if a single-target delete and that target is a directory; drives
   * the "empty folder" vs "Delete N items inside?" wording for single ops. */
  isDir: boolean
  /** Total descendants across all `names` — used to show recursion count. */
  childCount?: number
  pending?: boolean
  onConfirm: () => void
  onCancel: () => void
}

const PREVIEW_LIMIT = 3

/** Confirm dialog for file/folder deletion. For batches it lists the first
 * few names with an "...and N more" tail; for a single non-empty folder it
 * shows the descendant count so the user knows recursion is happening. */
export default function DeleteConfirmDialog({
  names,
  isDir,
  childCount = 0,
  pending,
  onConfirm,
  onCancel,
}: Props) {
  useEffect(() => pushEscHandler(onCancel), [onCancel])

  const isBatch = names.length > 1
  const headline = isBatch
    ? `Delete ${names.length} items?`
    : isDir
      ? childCount > 0
        ? `Delete "${names[0]}" and ${childCount} item${childCount === 1 ? '' : 's'} inside?`
        : `Delete the empty folder "${names[0]}"?`
      : `Delete "${names[0]}"?`

  const preview = isBatch ? names.slice(0, PREVIEW_LIMIT) : []
  const remaining = isBatch ? Math.max(0, names.length - PREVIEW_LIMIT) : 0

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs"
      onClick={onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[min(400px,90vw)] rounded-xl bg-white dark:bg-p-surface border border-p-border-light shadow-xl p-4"
      >
        <h3 className="text-sm font-semibold text-p-text mb-2">Confirm delete</h3>
        <p className="text-sm text-p-text-secondary mb-2">{headline}</p>
        {isBatch && (
          <ul className="text-xs text-p-text-light mb-4 space-y-0.5">
            {preview.map((n) => (
              <li key={n} className="truncate">• {n}</li>
            ))}
            {remaining > 0 && <li>…and {remaining} more</li>}
          </ul>
        )}
        <div className="flex items-center justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={pending}
            className="px-3 py-1.5 text-xs rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-p-text hover:bg-p-surface-hover"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={pending}
            className="px-3 py-1.5 text-xs rounded-lg bg-red-500 text-white hover:bg-red-600 disabled:opacity-60"
          >
            {pending ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
