import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { usePortalRoot } from '../ui/portalRoot'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT, POPOVER } from '../ui/classes'

import {
  isBatchRunning, useTransferStore, type TransferItem, type TransferMachineRow,
} from '../../store/transferStore'
import { Bar } from '../common/TransferBar'
import { fmtSize, pct } from '../common/transferFormat'

/** Anchored popover listing the current section's active uploads/syncs:
 * per item a phase-1 upload bar (while the XHR runs), then phase-2 rows —
 * ONE per target machine — with live byte progress; a workspace upload
 * batch still running has a Cancel above its items. Same portal +
 * outside-click mechanics as the toolbar's New File dropdown (portaled to
 * escape the toolbar's overflow clip). */

interface Props {
  items: TransferItem[]
  anchor: { left: number; top: number }
  onClose: () => void
}

function MachineRowView({ row }: { row: TransferMachineRow }) {
  const chip = {
    queued: 'bg-surface-muted text-secondary',
    active: 'bg-status-info-bg text-status-info-fg',
    done: 'bg-status-success-bg text-status-success-fg',
    failed: 'bg-status-danger-bg text-status-danger-fg',
  }[row.state]
  return (
    <div className="flex flex-col gap-0.5 pl-5 pr-1 py-0.5" title={row.error || undefined}>
      <div className="flex items-center gap-2 min-w-0">
        {/* monitor icon — a remote machine */}
        <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="shrink-0 text-tertiary">
          <rect x="2" y="4" width="20" height="13" rx="2" />
          <path strokeLinecap="round" d="M8 21h8m-4-4v4" />
        </svg>
        <span className="text-2xs text-primary truncate flex-1">{row.name}</span>
        <span className={`text-3xs px-1.5 py-px rounded-full font-medium shrink-0 ${chip}`}>
          {row.state === 'failed' ? 'failed' : row.state}
        </span>
      </div>
      {row.state === 'active' && (
        <Bar value={pct(row.bytesSent, row.bytesTotal)} />
      )}
      {row.state === 'failed' && (
        <span className="text-3xs text-tertiary">Retries at next sync</span>
      )}
    </div>
  )
}

function ItemView({ item }: { item: TransferItem }) {
  const uploading = item.phase === 1 && !item.uploadFailed
  const rows = Object.values(item.machines)
  const label = item.uploadFailed
    ? item.uploadError ? `Upload failed: ${item.uploadError}` : 'Upload failed'
    : uploading
      ? `Uploading… ${pct(item.uploadSent, item.uploadTotal)}%`
      : rows.length
        ? rows.every((r) => r.state === 'done')
          ? 'Synced to machines'
          : rows.some((r) => r.state === 'failed') && rows.every((r) => r.state === 'done' || r.state === 'failed')
            ? 'Sync incomplete'
            : 'Syncing to machines'
        : item.doneAt !== null
          ? 'Uploaded'
          : 'Processing…'
  return (
    <div className="flex flex-col gap-1 px-3 py-2 border-b border-subtle last:border-b-0">
      <div className="flex items-center gap-2 min-w-0">
        <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="shrink-0 text-secondary">
          <path strokeLinecap="round" strokeLinejoin="round" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M14 2v6h6" />
        </svg>
        <span className="text-xs text-primary font-medium truncate flex-1" title={item.relPath}>
          {item.filename}
        </span>
        <span className="text-3xs text-tertiary shrink-0">
          {fmtSize(item.uploadTotal)}
        </span>
      </div>
      <span className={`text-3xs ${item.uploadFailed ? 'text-status-danger-fg' : 'text-tertiary'}`}>
        {label}
      </span>
      {uploading && <Bar value={pct(item.uploadSent, item.uploadTotal)} />}
      {item.uploadFailed && <Bar value={100} failed />}
      {rows.map((r) => (
        <MachineRowView key={r.machineId} row={r} />
      ))}
    </div>
  )
}

/** One entry per batch with a row still uploading, named after its first
 * row's top folder ("files" for loose files). */
function runningBatches(items: TransferItem[]): Array<{ batchId: string; label: string }> {
  const out = new Map<string, string>()
  for (const t of items) {
    if (!isBatchRunning(t) || out.has(t.batchId!)) continue
    const first = items.find((i) => i.batchId === t.batchId)!
    const cut = first.filename.indexOf('/')
    out.set(t.batchId!, cut > 0 ? first.filename.slice(0, cut) : 'files')
  }
  return Array.from(out, ([batchId, label]) => ({ batchId, label }))
}

export default function TransferPopup({ items, anchor, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const portalRoot = usePortalRoot()

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  // Tick: prune lingering done items so the list (and, via the parent's
  // count, the icon) auto-hides shortly after everything finishes.
  useEffect(() => {
    const t = setInterval(() => useTransferStore.getState().prune(), 500)
    return () => clearInterval(t)
  }, [])

  return createPortal(
    <div
      ref={ref}
      style={{ position: 'fixed', left: anchor.left, top: anchor.top, zIndex: 60 }}
      className={`w-[300px] max-w-[calc(100vw-16px)] max-h-[50vh] overflow-y-auto ${POPOVER}`}
    >
      <div className="px-3 py-1.5 border-b border-subtle text-3xs font-semibold uppercase tracking-wide text-tertiary">
        Uploads and sync
      </div>
      {runningBatches(items).map(({ batchId, label }) => (
        <div key={batchId} className="flex items-center gap-2 px-3 py-1.5 border-b border-subtle">
          <span className="text-2xs text-primary truncate flex-1">Uploading {label}</span>
          <button
            type="button"
            onClick={() => useTransferStore.getState().cancelBatch(batchId)}
            className={`${BUTTON_BASE} ${BUTTON_VARIANT.secondary} ${BUTTON_SIZE.xs} shrink-0`}
          >
            Cancel
          </button>
        </div>
      ))}
      {items.length === 0 ? (
        <div className="px-3 py-3 text-2xs text-tertiary">Nothing in progress.</div>
      ) : (
        items.map((t) => <ItemView key={t.id} item={t} />)
      )}
    </div>,
    portalRoot,
  )
}
