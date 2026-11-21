import { Link } from 'react-router-dom'
import { ENGINE_CONNECT_REASONS } from '../../lib/engines/notice'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT } from '../ui/classes'

/** One sentence where an AI engine is needed (the proxy's refusal, or its
 *  chip on a task), with the way to connect one when that cures it. */
export default function EngineNotice({ message, reason, className = '' }: {
  message: string
  reason?: string
  className?: string
}) {
  const connect = !reason || ENGINE_CONNECT_REASONS.has(reason)
  return (
    <div
      role="status"
      data-testid="engine-notice"
      className={`flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 px-3 py-2 rounded-card border text-sm border-status-warning-border bg-status-warning-bg text-status-warning-fg ${className}`}
    >
      <span className="min-w-0">{message}</span>
      {connect && (
        <Link
          to="/user-settings?tab=ai-engines"
          className={`${BUTTON_BASE} ${BUTTON_VARIANT.primary} ${BUTTON_SIZE.xs} shrink-0`}
        >
          Connect an AI engine
        </Link>
      )}
    </div>
  )
}
