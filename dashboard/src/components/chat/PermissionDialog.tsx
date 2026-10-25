import { PAYLOAD, payloadText, toolPayload } from '../../lib/tools/roles'
import Button from '../ui/Button'

interface Props {
  requestId: string
  toolName: string
  toolInput: unknown
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

  // The call's payload is what the person judges: a shell's command, a file
  // tool's path (lib/tools/roles); everything else shows the whole input.
  const payload = toolPayload(toolName)
  const displayInput = (payload === PAYLOAD.COMMAND || payload === PAYLOAD.FILE_PATH)
    ? payloadText(toolName, toolInput) || inputStr
    : inputStr

  if (resolved) {
    // Approved: hide entirely (tool activity block already shows the result)
    if (approved) return null

    // Denied: show compact denial notice
    return (
      <div className="my-2 p-2 rounded-card border border-status-danger-border bg-status-danger-bg text-sm text-status-danger-fg">
        <div className="flex items-center gap-2">
          <span>{'\u2717'}</span>
          <span className="font-medium">{toolName}</span>
          <span className="text-xs">Denied</span>
        </div>
      </div>
    )
  }

  return (
    <div className="my-2 p-3 rounded-card border border-status-warning-border bg-status-warning-bg">
      {meetingAgent && (
        <div className="flex items-center gap-1.5 mb-1.5 text-xs text-secondary">
          <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: meetingAgent.color || 'var(--color-accent-agent-fill)' }} />
          <span className="font-medium">{meetingAgent.displayName || meetingAgent.slug}</span>
        </div>
      )}
      <div className="flex items-center gap-2 mb-2">
        <span className="text-status-warning-fg">&#128274;</span>
        <span className="font-medium text-sm text-primary">{toolName}</span>
        {description && <span className="text-xs text-secondary">{description}</span>}
      </div>
      <pre className="p-2 rounded-sm bg-surface-card border border-subtle text-xs text-primary mb-3 max-h-40 overflow-auto whitespace-pre-wrap">
        {displayInput}
      </pre>
      <div className="flex gap-2">
        <Button
          size="sm" variant="primary"
          onClick={() => onRespond(requestId, true)}
        >
          Allow
        </Button>
        <Button
          size="sm" variant="danger"
          onClick={() => onRespond(requestId, false)}
        >
          Deny
        </Button>
      </div>
    </div>
  )
}
