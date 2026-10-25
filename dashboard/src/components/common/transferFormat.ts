// The numbers beside a byte-transfer bar (TransferBar): the percent done
// and the size label, shared by the workspace transfers and the
// composer's attachment chips.

export function pct(sent: number, total: number): number {
  if (!total) return 0
  return Math.min(100, Math.round((sent / total) * 100))
}

export function fmtSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}
