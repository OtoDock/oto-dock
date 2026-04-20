import { useState, useEffect } from 'react'
import MarkdownContent from './MarkdownContent'

export default function PlanReviewCard({
  requestId,
  plan,
  toolInput,
  filename: proxyFilename,
  resolved,
  action: resolvedAction,
  onRespond,
  onSendMessage,
  onPlanFetched,
}: {
  requestId: string
  plan: string
  toolInput: any
  filename?: string
  resolved?: boolean
  action?: string
  onRespond?: (requestId: string, action: string) => void
  onSendMessage?: (text: string) => void
  onPlanFetched?: (filename: string, content: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [feedback, setFeedback] = useState('')

  // Use proxy-provided filename (consistent across edits), fall back to toolInput path
  const planFilePath = toolInput?.planFilePath || toolInput?.plan_file_path || ''
  const planFilename = proxyFilename
    || (planFilePath ? (planFilePath.split('/').pop() || planFilePath) : '')

  // Extract a display name from the plan's first heading or use filename
  const displayName = planFilename
    || (plan ? (plan.match(/^#\s+(.+)/m)?.[1]?.slice(0, 60) || 'Plan') : 'Plan')

  // Notify PlanPanel about this plan (only for unresolved — resolved ones are loaded from DB)
  useEffect(() => {
    if (plan && !resolved) {
      onPlanFetched?.(planFilename || `plan-${requestId.slice(0, 8)}.md`, plan)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestId])

  if (resolved) {
    const label =
      resolvedAction === 'implement_accept_edits' ? 'Implementing (Accept Edits)'
      : resolvedAction === 'implement_default' ? 'Implementing (Default)'
      : resolvedAction === 'edit' ? 'Editing plan...'
      : resolvedAction === 'reject' ? 'Plan cancelled'
      : 'Resolved'
    const color = resolvedAction === 'edit'
      ? 'text-brand bg-brand-50 border-brand/20'
      : resolvedAction === 'reject'
      ? 'text-p-text-secondary bg-p-surface border-p-border-light'
      : 'text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800'
    const icon = resolvedAction === 'edit' ? '\u270F\uFE0F'
      : resolvedAction === 'reject' ? '\u2716' : '\u2705'
    return (
      <div className={`my-2 p-3 rounded-lg border text-sm flex items-center gap-2 ${color}`}>
        <span>{icon}</span>
        {label}
      </div>
    )
  }

  const handleImplement = (action: string) => {
    onRespond?.(requestId, action)
    // Backend auto-queues "Please implement the plan now." on implement actions
  }

  const handleSubmitFeedback = () => {
    if (!feedback.trim()) return
    onRespond?.(requestId, 'edit')
    onSendMessage?.(`Please modify the plan: ${feedback.trim()}`)
    setEditing(false)
  }

  return (
    <div className="my-2 rounded-lg border border-brand/20 bg-brand-50 overflow-hidden">
      {/* Header */}
      <div className="px-3 py-2 border-b border-brand/20">
        <p className="text-sm font-medium text-brand">
          Plan Ready for Review: {displayName}
        </p>
      </div>

      {/* Plan content */}
      {plan ? (
        <div className="px-4 py-3 border-b border-brand/20 bg-white dark:bg-p-surface max-h-96 overflow-y-auto">
          <MarkdownContent className="prose-headings:text-brand prose-li:text-gray-800 dark:prose-li:text-gray-200">
            {plan}
          </MarkdownContent>
        </div>
      ) : (
        <div className="px-4 py-3 border-b border-brand/20 text-sm text-brand/60">
          No plan content available
        </div>
      )}

      {/* Edit feedback input */}
      {editing && (
        <div className="px-4 py-3 border-b border-brand/20 bg-white dark:bg-p-surface">
          <textarea
            autoFocus
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                handleSubmitFeedback()
              }
            }}
            placeholder="Describe what to change in the plan..."
            className="w-full px-3 py-2 text-sm border border-brand/30 rounded-lg resize-none
                       bg-white dark:bg-p-surface text-p-text placeholder:text-p-text-light
                       focus:outline-hidden focus:ring-1 focus:ring-brand"
            rows={3}
          />
          <div className="flex gap-2 mt-2">
            <button
              onClick={handleSubmitFeedback}
              disabled={!feedback.trim()}
              className="px-3 py-1 rounded-lg text-sm font-medium text-white bg-brand hover:bg-brand-hover disabled:opacity-50 transition-colors"
            >
              Send Feedback
            </button>
            <button
              onClick={() => setEditing(false)}
              className="px-3 py-1 rounded-lg text-sm font-medium text-gray-600 dark:text-gray-300 bg-white dark:bg-p-surface border border-gray-300 dark:border-p-border-light hover:bg-gray-50 dark:hover:bg-p-surface-hover transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Action buttons */}
      {!editing && (
        <div className="flex flex-wrap gap-2 px-4 py-3">
          {onRespond && (
            <>
              <button
                onClick={() => handleImplement('implement_accept_edits')}
                className="px-3 py-1.5 rounded-lg text-sm font-medium text-white bg-brand hover:bg-brand-hover transition-colors"
              >
                Implement (Accept Edits)
              </button>
              <button
                onClick={() => handleImplement('implement_default')}
                className="px-3 py-1.5 rounded-lg text-sm font-medium text-brand bg-brand-100 hover:bg-brand-surface transition-colors"
              >
                Implement (Default)
              </button>
            </>
          )}
          {onRespond && (
            <>
              <button
                onClick={() => setEditing(true)}
                className="px-3 py-1.5 rounded-lg text-sm font-medium text-brand bg-white dark:bg-p-surface border border-brand/30 hover:bg-brand-50 transition-colors"
              >
                Edit Plan
              </button>
              <button
                onClick={() => onRespond(requestId, 'reject')}
                className="px-3 py-1.5 rounded-lg text-sm font-medium text-gray-600 dark:text-gray-300 bg-white dark:bg-p-surface border border-gray-300 dark:border-p-border-light hover:bg-gray-50 dark:hover:bg-p-surface-hover transition-colors"
              >
                Cancel
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
