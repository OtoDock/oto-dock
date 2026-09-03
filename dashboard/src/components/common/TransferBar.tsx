// Shared progress-bar primitives for byte transfers. Extracted from
// TransferPopup (the workspace overlay) so the chat composer's attachment
// chips render the identical bar without importing a workspace component.

export function pct(sent: number, total: number): number {
  if (!total) return 0
  return Math.min(100, Math.round((sent / total) * 100))
}

export function fmtSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

export function Bar({ value, failed }: { value: number; failed?: boolean }) {
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
