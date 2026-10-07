/**
 * "transferred from <person>" on a task, trigger or notification row that
 * the offboarding transfer moved to its current owner when its first
 * creator lost their standing on the agent. Renders nothing for a row that
 * never changed hands; the date is the tooltip.
 */
export interface TransferredRow {
  transferred_from?: string
  transferred_at?: string
  transferred_from_name?: string
}

export default function TransferredFrom({ row, className = '' }: { row: TransferredRow; className?: string }) {
  if (!row.transferred_from) return null
  const who = row.transferred_from_name || 'a removed person'
  const when = row.transferred_at ? new Date(row.transferred_at).toLocaleString() : ''
  return (
    <span
      className={`text-xs text-p-text-secondary ${className}`}
      title={when ? `Moved to its current owner on ${when}` : undefined}
      data-testid="transferred-from"
    >
      transferred from {who}
    </span>
  )
}
