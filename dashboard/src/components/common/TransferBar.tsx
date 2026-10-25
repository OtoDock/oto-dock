// Shared progress-bar primitives for byte transfers. Extracted from
// TransferPopup (the workspace overlay) so the chat composer's attachment
// chips render the identical bar without importing a workspace component.

export function Bar({ value, failed }: { value: number; failed?: boolean }) {
  return (
    <div className="h-1 w-full rounded-full bg-surface-muted overflow-hidden">
      <div
        className={`h-full rounded-full transition-[width] duration-300 ${
          failed ? 'bg-status-danger-solid' : 'bg-action'
        }`}
        style={{ width: `${value}%` }}
      />
    </div>
  )
}
