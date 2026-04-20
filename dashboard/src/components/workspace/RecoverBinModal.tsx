import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { pushEscHandler } from '../../lib/escStack'
import {
  useRestoreFiles,
  useDiscardFiles,
  type RecoverBinEntry,
  type RestoreResult,
} from '../../api/agents'

interface Props {
  agent: string
  entries: RecoverBinEntry[]
  onClose: () => void
}

const REASON_LABEL: Record<RecoverBinEntry['reason'], string> = {
  deleted: 'Deleted',
  overwritten: 'Replaced by a newer version',
  reconciled: 'Removed',
}

function parentFolder(relPath: string): string {
  const i = relPath.lastIndexOf('/')
  return i === -1 ? '(root)' : relPath.slice(0, i)
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Recover files that were removed or replaced. Entries are grouped by their
 * parent folder; every one starts selected and can be deselected. Restoring
 * returns each file to its original path — if something already occupies that
 * path the recovered copy is saved alongside as "name (recovered).ext" so
 * existing content is never overwritten. */
export default function RecoverBinModal({ agent, entries, onClose }: Props) {
  useEffect(() => pushEscHandler(onClose), [onClose])

  const restore = useRestoreFiles()
  const discard = useDiscardFiles()
  const [result, setResult] = useState<RestoreResult | null>(null)
  const [armDiscard, setArmDiscard] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(entries.map((e) => e.entry_id)),
  )
  // Re-arm the destructive Discard whenever the selection changes.
  useEffect(() => setArmDiscard(false), [selected])

  // Group by parent folder; folders sorted, files sorted within each.
  const groups = useMemo(() => {
    const m = new Map<string, RecoverBinEntry[]>()
    for (const e of entries) {
      const f = parentFolder(e.rel_path)
      const arr = m.get(f)
      if (arr) arr.push(e)
      else m.set(f, [e])
    }
    return [...m.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(
        ([folder, items]) =>
          [
            folder,
            [...items].sort((a, b) =>
              a.original_name.localeCompare(b.original_name),
            ),
          ] as [string, RecoverBinEntry[]],
      )
  }, [entries])

  const allIds = useMemo(() => entries.map((e) => e.entry_id), [entries])
  const allSelected = selected.size === allIds.length

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const setGroup = (items: RecoverBinEntry[], on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev)
      for (const e of items) {
        if (on) next.add(e.entry_id)
        else next.delete(e.entry_id)
      }
      return next
    })

  const onRestore = async () => {
    if (selected.size === 0) return
    const res = await restore.mutateAsync({ agent, entryIds: [...selected] })
    setResult(res)
  }

  const onDiscard = async () => {
    if (selected.size === 0) return
    if (!armDiscard) {
      setArmDiscard(true)  // first click arms; second click confirms
      return
    }
    await discard.mutateAsync({ agent, entryIds: [...selected] })
    onClose()
  }

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs p-3"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[min(560px,92vw)] max-h-[80vh] flex flex-col rounded-xl bg-white dark:bg-p-surface border border-p-border-light shadow-xl"
      >
        {/* Header */}
        <div className="p-4 border-b border-p-border-light">
          <h3 className="text-sm font-semibold text-p-text">Recover files</h3>
          <p className="text-xs text-p-text-secondary mt-1">
            {result
              ? 'All done.'
              : "These files were removed or replaced by a newer version. Choose which to restore — each goes back to its original location. If a file is already there, the recovered copy is saved alongside it (nothing is overwritten)."}
          </p>
        </div>

        {/* Body */}
        {result ? (
          <div className="flex-1 overflow-y-auto p-4 min-h-0">
            <p className="text-sm text-p-text mb-2">
              Restored {result.restored.length} file
              {result.restored.length === 1 ? '' : 's'}.
            </p>
            {result.renamed.length > 0 && (
              <div className="mb-2">
                <p className="text-xs text-p-text-secondary mb-1">
                  Some files already existed, so the recovered copies were saved
                  alongside them:
                </p>
                <ul className="text-[11px] text-p-text-light space-y-0.5">
                  {result.renamed.map((r) => (
                    <li key={r.entry_id} className="truncate">
                      {r.restored_as}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {result.denied.length > 0 && (
              <p className="text-xs text-amber-600">
                {result.denied.length} file
                {result.denied.length === 1 ? '' : 's'} could not be restored.
              </p>
            )}
          </div>
        ) : (
          <>
            {/* Select-all bar */}
            <div className="flex items-center justify-between px-4 py-2 border-b border-p-border-light shrink-0">
              <span className="text-xs text-p-text-light">
                {selected.size} of {allIds.length} selected
              </span>
              <button
                onClick={() => setSelected(allSelected ? new Set() : new Set(allIds))}
                className="text-xs text-brand hover:underline"
              >
                {allSelected ? 'Deselect all' : 'Select all'}
              </button>
            </div>

            {/* Grouped, checkbox list */}
            <div className="flex-1 overflow-y-auto px-2 py-2 min-h-0">
              {groups.map(([folder, items]) => {
                const groupAllOn = items.every((e) => selected.has(e.entry_id))
                return (
                  <div key={folder} className="mb-3">
                    <div className="flex items-center justify-between px-2 mb-1">
                      <span className="text-[11px] font-medium text-p-text-secondary truncate min-w-0">
                        {folder}
                      </span>
                      <button
                        onClick={() => setGroup(items, !groupAllOn)}
                        className="text-[11px] text-brand hover:underline shrink-0 ml-2"
                      >
                        {groupAllOn ? 'none' : 'all'}
                      </button>
                    </div>
                    <ul className="space-y-0.5">
                      {items.map((e) => (
                        <li key={e.entry_id}>
                          <label className="flex items-center gap-2 px-2 py-1 rounded-sm hover:bg-p-surface-hover cursor-pointer">
                            <input
                              type="checkbox"
                              checked={selected.has(e.entry_id)}
                              onChange={() => toggle(e.entry_id)}
                              className="shrink-0"
                            />
                            <span className="text-xs text-p-text truncate min-w-0 flex-1">
                              {e.original_name}
                            </span>
                            <span className="text-[10px] text-p-text-light shrink-0">
                              {REASON_LABEL[e.reason]} · {formatSize(e.size)}
                            </span>
                          </label>
                        </li>
                      ))}
                    </ul>
                  </div>
                )
              })}
            </div>
          </>
        )}

        {/* Footer */}
        <div className="flex items-center justify-between gap-2 p-4 border-t border-p-border-light shrink-0">
          {result ? (
            <button
              onClick={onClose}
              className="ml-auto px-3 py-1.5 text-xs rounded-lg bg-brand text-white hover:opacity-90"
            >
              Done
            </button>
          ) : (
            <>
              <button
                onClick={onDiscard}
                disabled={discard.isPending || selected.size === 0}
                title="Permanently remove the selected backups without restoring them"
                className={
                  armDiscard
                    ? 'px-3 py-1.5 text-xs rounded-lg bg-red-500 text-white hover:bg-red-600 disabled:opacity-60'
                    : 'px-3 py-1.5 text-xs rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover disabled:opacity-60'
                }
              >
                {discard.isPending
                  ? 'Discarding…'
                  : armDiscard
                    ? `Discard ${selected.size} permanently?`
                    : 'Discard'}
              </button>
              <div className="flex items-center gap-2">
                <button
                  onClick={onClose}
                  disabled={restore.isPending || discard.isPending}
                  className="px-3 py-1.5 text-xs rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-p-text hover:bg-p-surface-hover"
                >
                  Cancel
                </button>
                <button
                  onClick={onRestore}
                  disabled={restore.isPending || selected.size === 0}
                  className="px-3 py-1.5 text-xs rounded-lg bg-brand text-white hover:opacity-90 disabled:opacity-60"
                >
                  {restore.isPending
                    ? 'Restoring…'
                    : `Restore ${selected.size} selected`}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
