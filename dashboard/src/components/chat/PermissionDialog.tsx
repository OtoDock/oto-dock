interface Props {
  requestId: string
  toolName: string
  toolInput: any
  description?: string
  resolved?: boolean
  approved?: boolean
  meetingAgent?: { slug: string; displayName: string; color: string }
  onRespond: (requestId: string, approved: boolean) => void
}

export default function PermissionDialog({
  requestId,
  toolName,
  toolInput,
  description,
  resolved,
  approved,
  meetingAgent,
  onRespond,
}: Props) {
  const inputStr =
    typeof toolInput === 'string' ? toolInput : JSON.stringify(toolInput, null, 2)

  // Show command for Bash tool, file path for Read/Write/Edit
  const isBash = toolName === 'Bash'
  const isFileOp = ['Read', 'Write', 'Edit'].includes(toolName)
  const displayInput = isBash
    ? toolInput?.command || inputStr
    : isFileOp
      ? toolInput?.file_path || inputStr
      : inputStr

  if (resolved) {
    // Approved: hide entirely (tool activity block already shows the result)
    if (approved) return null

    // Denied: show compact denial notice
    return (
      <div className="my-2 p-2 rounded-lg border border-p-accent-red/20 bg-p-accent-red/5 text-sm text-p-accent-red">
        <div className="flex items-center gap-2">
          <span>{'\u2717'}</span>
          <span className="font-medium">{toolName}</span>
          <span className="text-xs">Denied</span>
        </div>
      </div>
    )
  }

  return (
    <div className="my-2 p-3 rounded-lg border border-[#f4b206]/30 bg-[#f4b206]/5">
      {meetingAgent && (
        <div className="flex items-center gap-1.5 mb-1.5 text-xs text-p-text-secondary">
          <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: meetingAgent.color || '#6B7280' }} />
          <span className="font-medium">{meetingAgent.displayName || meetingAgent.slug}</span>
        </div>
      )}
      <div className="flex items-center gap-2 mb-2">
        <span className="text-[#b8860b]">&#128274;</span>
        <span className="font-medium text-sm text-p-text">{toolName}</span>
        {description && <span className="text-xs text-p-text-secondary">{description}</span>}
      </div>
      <pre className="p-2 rounded-sm bg-white dark:bg-p-surface border border-p-border-light text-xs text-p-text mb-3 max-h-40 overflow-auto whitespace-pre-wrap">
        {displayInput}
      </pre>
      <div className="flex gap-2">
        <button
          onClick={() => onRespond(requestId, true)}
          className="px-3 py-1 rounded-lg text-sm font-medium text-white bg-green-600 hover:bg-green-700"
        >
          Allow
        </button>
        <button
          onClick={() => onRespond(requestId, false)}
          className="px-3 py-1 rounded-lg text-sm font-medium text-white bg-red-600 hover:bg-red-700"
        >
          Deny
        </button>
      </div>
    </div>
  )
}
