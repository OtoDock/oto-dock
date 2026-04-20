import { useEffect, useState } from 'react'
import type { ToastItem } from '../../../hooks/useNotifications'
import NotificationBody from './NotificationBody'

interface Props {
  toasts: ToastItem[]
  onDismiss: (id: string) => void
  onStopAlarm?: () => void
  onNavigate?: (agentSlug: string, chatId: string) => void
}

const SEVERITY_STYLES: Record<string, string> = {
  info: 'border-l-brand bg-brand-surface/80',
  success: 'border-l-p-accent-teal bg-p-accent-teal/5',
  warning: 'border-l-p-accent-yellow bg-p-accent-yellow/5',
  danger: 'border-l-p-accent-red bg-p-accent-red/5',
}

const SEVERITY_TITLE_COLOR: Record<string, string> = {
  info: 'text-brand',
  success: 'text-p-accent-teal',
  warning: 'text-[#b8860b]',
  danger: 'text-p-accent-red',
}

function ToastCard({ toast, onDismiss, onStopAlarm, onNavigate }: { toast: ToastItem; onDismiss: (id: string) => void; onStopAlarm?: () => void; onNavigate?: (agentSlug: string, chatId: string) => void }) {
  const [visible, setVisible] = useState(false)
  const { delivery } = toast
  const hasLink = !!(delivery.agent_slug && delivery.chat_id)

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
      onNavigate(delivery.agent_slug!, delivery.chat_id!)
      handleDismiss()
    }
  }

  return (
    <div
      className={`w-full rounded-lg shadow-lg border border-p-border-light/50 border-l-[4px] backdrop-blur-xs
                  transition-all duration-200 ease-out
                  ${SEVERITY_STYLES[delivery.severity] || SEVERITY_STYLES.info}
                  ${visible ? 'translate-x-0 opacity-100' : 'translate-x-full opacity-0'}
                  ${delivery.severity === 'danger' ? 'animate-pulse' : ''}
                  ${hasLink ? 'cursor-pointer' : ''}`}
      onClick={handleClick}
    >
      <div className="flex items-start gap-2 p-3">
        <div className="flex-1 min-w-0">
          <p className={`text-sm font-semibold ${SEVERITY_TITLE_COLOR[delivery.severity] || 'text-p-text'}`}>
            {delivery.title}
          </p>
          <NotificationBody body={delivery.body} clampClass="line-clamp-3" />
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); handleDismiss() }}
          className="shrink-0 w-5 h-5 flex items-center justify-center text-p-text-light hover:text-p-text transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>
  )
}

export default function NotificationToast({ toasts, onDismiss, onStopAlarm, onNavigate }: Props) {
  if (toasts.length === 0) return null

  return (
    <div className="fixed top-14 right-3 left-3 sm:left-auto sm:right-4 sm:w-80 z-50 flex flex-col gap-2">
      {toasts.map(toast => (
        <ToastCard
          key={toast.id}
          toast={toast}
          onDismiss={onDismiss}
          onStopAlarm={onStopAlarm}
          onNavigate={onNavigate}
        />
      ))}
    </div>
  )
}
