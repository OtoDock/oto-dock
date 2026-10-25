import { useEffect, useState } from 'react'
import type { ToastItem } from '../../../hooks/useNotifications'
import type { AgentSummary } from '../../../api/agents'
import NotificationBody from './NotificationBody'
import NotificationAgentHeader from './NotificationAgentHeader'
import { ToastPortal } from '../../ui/Toast'
import { TOAST } from '../../ui/classes'

interface Props {
  toasts: ToastItem[]
  /** Cached agents list — resolves each toast's `agent_slug` to its avatar + name. */
  agents?: AgentSummary[]
  onDismiss: (id: string) => void
  onStopAlarm?: () => void
  onNavigate?: (agentSlug: string, chatId: string, href?: string) => void
}

// A toast is an overlay card; its severity is the edge (the solid) and the
// title (the foreground), never a tinted fill.
const SEVERITY_STYLES: Record<string, string> = {
  info: 'border-l-status-info-solid',
  success: 'border-l-status-success-solid',
  warning: 'border-l-status-warning-solid',
  danger: 'border-l-status-danger-solid',
}

// What a screen reader does with a card: a warning or a danger interrupts.
const SEVERITY_ROLE: Record<string, 'status' | 'alert'> = {
  info: 'status',
  success: 'status',
  warning: 'alert',
  danger: 'alert',
}

const SEVERITY_TITLE_COLOR: Record<string, string> = {
  info: 'text-status-info-fg',
  success: 'text-status-success-fg',
  warning: 'text-status-warning-fg',
  danger: 'text-status-danger-fg',
}

function ToastCard({ toast, agents, onDismiss, onStopAlarm, onNavigate }: { toast: ToastItem; agents?: AgentSummary[]; onDismiss: (id: string) => void; onStopAlarm?: () => void; onNavigate?: (agentSlug: string, chatId: string, href?: string) => void }) {
  const [visible, setVisible] = useState(false)
  const { delivery } = toast
  const hasLink = !!delivery.href || !!(delivery.agent_slug && delivery.chat_id)

  // Slide-in animation
  useEffect(() => {
    requestAnimationFrame(() => setVisible(true))
  }, [])

  const handleDismiss = () => {
    setVisible(false)
    if (delivery.severity === 'danger' && onStopAlarm) onStopAlarm()
    setTimeout(() => onDismiss(toast.id), 200)
  }

  const handleClick = () => {
    if (hasLink && onNavigate) {
      onNavigate(delivery.agent_slug || '', delivery.chat_id || '', delivery.href || undefined)
      handleDismiss()
    }
  }

  return (
    <div
      role={SEVERITY_ROLE[delivery.severity] ?? 'status'}
      className={`w-full ${TOAST} border-l-4
                  transition-all duration-base ease-enter
                  ${SEVERITY_STYLES[delivery.severity] || SEVERITY_STYLES.info}
                  ${visible ? 'translate-x-0 opacity-100' : 'translate-x-full opacity-0'}
                  ${delivery.severity === 'danger' ? 'animate-pulse' : ''}
                  ${hasLink ? 'cursor-pointer' : ''}`}
      onClick={handleClick}
    >
      <div className="flex items-start gap-2 p-3">
        <div className="flex-1 min-w-0">
          <div className="mb-0.5">
            <NotificationAgentHeader agentSlug={delivery.agent_slug} agents={agents} />
          </div>
          <p className={`text-sm font-semibold ${SEVERITY_TITLE_COLOR[delivery.severity] || 'text-primary'}`}>
            {delivery.title}
          </p>
          <NotificationBody body={delivery.body} clampClass="line-clamp-3" />
        </div>
        <button
          type="button"
          aria-label="Dismiss"
          onClick={(e) => { e.stopPropagation(); handleDismiss() }}
          className="shrink-0 w-5 h-5 flex items-center justify-center text-tertiary hover:text-primary transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>
  )
}

/** The cards, rendered into the page's one toast region (`ui/Toast.tsx`). */
export default function NotificationToast({ toasts, agents, onDismiss, onStopAlarm, onNavigate }: Props) {
  if (toasts.length === 0) return null

  return (
    <ToastPortal>
      {toasts.map(toast => (
        <ToastCard
          key={toast.id}
          toast={toast}
          agents={agents}
          onDismiss={onDismiss}
          onStopAlarm={onStopAlarm}
          onNavigate={onNavigate}
        />
      ))}
    </ToastPortal>
  )
}
