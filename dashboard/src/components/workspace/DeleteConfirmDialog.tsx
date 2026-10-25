import AlertDialog from '../ui/AlertDialog'

interface Props {
  /** All targets — usually one, multiple for batch delete. */
  names: string[]
  /** True if a single-target delete and that target is a directory; drives
   * the "empty folder" vs "Delete N items inside?" wording for single ops. */
  isDir: boolean
  /** Total descendants across all `names` — used to show recursion count. */
  childCount?: number
  /** Files in the selection too large for the Recover bin: their deletion
   * cannot be undone (the server skips capture above the bin cap). > 0
   * renders a prominent warning line. */
  binSkippedCount?: number
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
  binSkippedCount = 0,
  pending = false,
  onConfirm,
  onCancel,
}: Props) {
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

  return (
    <AlertDialog
      title="Confirm delete"
      confirmLabel="Delete"
      tone="danger"
      busy={pending}
      busyLabel="Deleting…"
      onConfirm={onConfirm}
      onCancel={onCancel}
    >
      <p className="mb-2">{headline}</p>
      {isBatch && (
        <ul className="text-xs text-tertiary mb-2 space-y-0.5">
          {preview.map((n) => (
            <li key={n} className="truncate">• {n}</li>
          ))}
          {remaining > 0 && <li>…and {remaining} more</li>}
        </ul>
      )}
      {binSkippedCount > 0 && (
        <p className="text-xs text-status-warning-fg">
          {binSkippedCount === 1
            ? 'One file is too large for the Recover bin. Deleting it cannot be undone.'
            : `${binSkippedCount} files are too large for the Recover bin. Deleting them cannot be undone.`}
        </p>
      )}
    </AlertDialog>
  )
}
