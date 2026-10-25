import { useState, useEffect } from 'react'
import MarkdownContent from './MarkdownContent'
import { usePrimaryPointerCoarse } from '../../hooks/usePrimaryPointerCoarse'
import { usePasteGuard } from '../../hooks/usePasteGuard'
import { enterAction, insertNewline } from '../../lib/composerKeys'
import { inputText } from '../../lib/tools/input'
import { IMPLEMENT_ASK, IMPLEMENT_AUTO, PLAN_ACTIONS_BEFORE_1_9 } from '../../lib/engines/modes'
import { PLAN_IMPLEMENTED, PLAN_REVISED } from '../../lib/messageBlocks'
import Button from '../ui/Button'

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
  toolInput: unknown
  filename?: string
  resolved?: boolean
  action?: string
  onRespond?: (requestId: string, action: string) => void
  onSendMessage?: (text: string) => void
  onPlanFetched?: (filename: string, content: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [feedback, setFeedback] = useState('')
  // The composer's key rule (lib/composerKeys): Enter adds a line here too.
  const touchPrimary = usePrimaryPointerCoarse()
  const pasteGuard = usePasteGuard()

  // Use proxy-provided filename (consistent across edits), fall back to toolInput path
  const planFilePath = inputText(toolInput, 'planFilePath') || inputText(toolInput, 'plan_file_path')
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
    const action = PLAN_ACTIONS_BEFORE_1_9[resolvedAction ?? ''] ?? resolvedAction
    const label =
      action === IMPLEMENT_AUTO ? 'Implementing in Auto'
      : action === IMPLEMENT_ASK ? 'Implementing in Ask'
      : action === 'edit' ? 'Editing plan...'
      : action === PLAN_REVISED ? 'Plan revised'
      : action === PLAN_IMPLEMENTED ? 'Plan implemented'
      : action === 'reject' ? 'Plan cancelled'
      : 'Resolved'
    const edited = resolvedAction === 'edit' || resolvedAction === PLAN_REVISED
    const color = edited
      ? 'text-accent-agent-fg bg-accent-agent-bg border-accent-agent-border'
      : resolvedAction === 'reject'
      ? 'text-secondary bg-surface-muted border-subtle'
      : 'text-status-success-fg bg-status-success-bg border-status-success-border'
    const icon = edited ? '\u270F\uFE0F'
      : resolvedAction === 'reject' ? '\u2716' : '\u2705'
    return (
      <div className={`my-2 p-3 rounded-card border text-sm flex items-center gap-2 ${color}`}>
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
    <div className="my-2 rounded-card bg-surface-card shadow-elevation-1 overflow-hidden">
      {/* Header */}
      <div className="px-3 py-2 border-b border-subtle">
        <p className="text-sm font-medium text-accent-agent-fg">
          Plan Ready for Review: {displayName}
        </p>
      </div>

      {/* Plan content */}
      {plan ? (
        <div className="px-4 py-3 border-b border-subtle max-h-96 overflow-y-auto">
          <MarkdownContent className="prose-li:text-primary">
            {plan}
          </MarkdownContent>
        </div>
      ) : (
        <div className="px-4 py-3 border-b border-subtle text-sm text-tertiary">
          No plan content available
        </div>
      )}

      {/* Edit feedback input */}
      {editing && (
        <div className="px-4 py-3 border-b border-subtle">
          <textarea
            autoFocus
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            onKeyDown={(e) => {
              pasteGuard.onKeyDown(e)
              const action = enterAction(e.nativeEvent, { coarse: touchPrimary, pasteHeld: pasteGuard.held() })
              if (action !== 'pass') e.preventDefault()
              if (action === 'newline') insertNewline(e.currentTarget, setFeedback)
              if (action === 'send') handleSubmitFeedback()
            }}
            onKeyUp={pasteGuard.onKeyUp}
            onPaste={pasteGuard.onPaste}
            onBlur={pasteGuard.onBlur}
            placeholder="Describe what to change in the plan..."
            className="w-full px-3 py-2 text-sm border border-strong rounded-control resize-none
                       bg-surface-card text-primary placeholder:text-tertiary
                       focus:outline-hidden focus:ring-1 focus:ring-focus-ring"
            rows={3}
          />
          <div className="flex gap-2 mt-2">
            <Button
              size="sm" variant="primary"
              onClick={handleSubmitFeedback}
              disabled={!feedback.trim()}
              title={touchPrimary ? undefined : 'Send Feedback: Shift+Enter'}
            >
              Send Feedback
            </Button>
            <Button
              size="sm"
              onClick={() => setEditing(false)}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}

      {/* Action buttons */}
      {!editing && (
        <div className="flex flex-wrap gap-2 px-4 py-3">
          {onRespond && (
            <>
              <Button
                size="sm" variant="primary"
                onClick={() => handleImplement(IMPLEMENT_AUTO)}
              >
                Implement in Auto
              </Button>
              <Button
                size="sm"
                onClick={() => handleImplement(IMPLEMENT_ASK)}
              >
                Implement in Ask
              </Button>
            </>
          )}
          {onRespond && (
            <>
              <Button
                size="sm"
                onClick={() => setEditing(true)}
              >
                Edit Plan
              </Button>
              <Button
                size="sm"
                onClick={() => onRespond(requestId, 'reject')}
              >
                Cancel
              </Button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
