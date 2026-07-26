import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

import { useTransferStore, type TransferItem, type TransferMachineRow } from '../../store/transferStore'

/** Anchored popover listing the current section's active uploads/syncs:
 * per item a phase-1 upload bar (while the XHR runs), then phase-2 rows —
 * ONE per target machine — with live byte progress. Same portal +
 * outside-click mechanics as the toolbar's New File dropdown (portaled to
 * escape the toolbar's overflow clip). */

interface Props {
  items: TransferItem[]
  anchor: { left: number; top: number }
  onClose: () => void
}

function pct(sent: number, total: number): number {
  if (!total) return 0
  return Math.min(100, Math.round((sent / total) * 100))
}

function fmtSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

function Bar({ value, failed }: { value: number; failed?: boolean }) {
  return (
    <div className="h-1 w-full rounded-full bg-p-surface-hover overflow-hidden">
      <div
        className={`h-full rounded-full transition-[width] duration-300 ${
          failed ? 'bg-red-500' : 'bg-brand'
        }`}
        style={{ width: `${value}%` }}
      />
    </div>
  )
}

function MachineRowView({ row }: { row: TransferMachineRow }) {
  const chip = {
    queued: 'bg-p-surface-hover text-p-text-secondary',
    active: 'bg-brand-surface text-brand',
    done: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-400',
    failed: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-400',
  }[row.state]
  return (
    <div className="flex flex-col gap-0.5 pl-5 pr-1 py-0.5" title={row.error || undefined}>
      <div className="flex items-center gap-2 min-w-0">
        {/* monitor icon — a remote machine */}
        <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="shrink-0 text-p-text-light">
          <rect x="2" y="4" width="20" height="13" rx="2" />
          <path strokeLinecap="round" d="M8 21h8m-4-4v4" />
        </svg>
        <span className="text-[11px] text-p-text truncate flex-1">{row.name}</span>
        <span className={`text-[9px] px-1.5 py-px rounded-full font-medium shrink-0 ${chip}`}>
          {row.state === 'failed' ? 'failed' : row.state}
        </span>
      </div>
      {row.state === 'active' && (
        <Bar value={pct(row.bytesSent, row.bytesTotal)} />
      )}
      {row.state === 'failed' && (
        <span className="text-[10px] text-p-text-light">retries at next sync</span>
      )}
    </div>
  )
}

function ItemView({ item }: { item: TransferItem }) {
  const uploading = item.phase === 1 && !item.uploadFailed
  const rows = Object.values(item.machines)
  const label = item.uploadFailed
    ? 'Upload failed'
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
    <div className="flex flex-col gap-1 px-3 py-2 border-b border-p-border-light last:border-b-0">
      <div className="flex items-center gap-2 min-w-0">
        <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="shrink-0 text-p-text-secondary">
          <path strokeLinecap="round" strokeLinejoin="round" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M14 2v6h6" />
        </svg>
        <span className="text-xs text-p-text font-medium truncate flex-1" title={item.relPath}>
          {item.filename}
        </span>
        <span className="text-[10px] text-p-text-light shrink-0">
          {fmtSize(item.uploadTotal)}
        </span>
      </div>
      <span className={`text-[10px] ${item.uploadFailed ? 'text-red-600' : 'text-p-text-light'}`}>
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

export default function TransferPopup({ items, anchor, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null)

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
      className="w-[300px] max-w-[calc(100vw-16px)] max-h-[50vh] overflow-y-auto bg-white dark:bg-p-surface rounded-lg border border-p-border-light shadow-lg"
    >
      <div className="px-3 py-1.5 border-b border-p-border-light text-[10px] font-semibold uppercase tracking-wide text-p-text-light">
        Uploads &amp; sync
      </div>
      {items.length === 0 ? (
        <div className="px-3 py-3 text-[11px] text-p-text-light">Nothing in progress.</div>
      ) : (
        items.map((t) => <ItemView key={t.id} item={t} />)
      )}
    </div>,
    document.body,
  )
}
