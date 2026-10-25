import type { OpenAppFrame } from '../../lib/appLive'
import Button from '../ui/Button'
import { TOAST } from '../ui/classes'

/**
 * The notice an agent's open_app leaves on a page that cannot show the app
 * itself (another agent's chat, the full-screen page of another app): the
 * same corner and card as the notification toasts, one card per app, an
 * Open button that goes to the app's own page. Nothing navigates on its own.
 */

interface Props {
  items: OpenAppFrame[]
  onOpen: (f: OpenAppFrame) => void
  onDismiss: (appId: string) => void
}

export default function OpenAppToast({ items, onOpen, onDismiss }: Props) {
  if (!items.length) return null
  return (
    <div
      className="fixed top-14 right-3 left-3 sm:left-auto sm:right-4 sm:w-80 z-50 flex flex-col gap-2"
      data-testid="open-app-toast"
    >
      {items.map((f) => (
        <div
          key={f.app_id}
          className={`w-full ${TOAST} border-l-4 border-l-action`}
        >
          <div className="flex items-start gap-2 p-3">
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-primary truncate">{f.title || 'An app'}</p>
              <p className="text-sm text-secondary">
                The agent <span className="font-medium text-primary">{f.opened_by || f.agent}</span> wants to show you this app.
              </p>
              <Button variant="primary" size="xs" className="mt-2" onClick={() => onOpen(f)}>
                Open
              </Button>
            </div>
            <button
              onClick={() => onDismiss(f.app_id)}
              aria-label="Dismiss"
              className="shrink-0 w-5 h-5 flex items-center justify-center text-tertiary hover:text-primary transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}
