import { useState, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface Props {
  action: 'enter' | 'exit'
  toolInput?: any
  superseded?: boolean
  onImplement?: (planPath: string, mode: string) => void
  // Codex has no plan FILE (the plan is the turn's final message) — implement
  // switches the permission mode + sends the build turn instead of implement_plan.
  onImplementCodex?: (mode: string) => void
  onSendMessage?: (text: string) => void
  onPlanFetched?: (filename: string, content: string) => void
}

export default function PlanView({ action, toolInput, superseded, onImplement, onImplementCodex, onSendMessage, onPlanFetched }: Props) {
  const [collapsed, setCollapsed] = useState(false)

  // ExitPlanMode tool_input has: planFilePath, plan (content), allowedPrompts
  const planPath = toolInput?.planFilePath || toolInput?.plan_file_path || toolInput?.file_path || ''
  const planFilename = planPath ? (planPath.split('/').pop() || planPath) : ''
  // Plan content comes directly from the tool_input — no API fetch needed
  const planContent = toolInput?.plan || ''
  // Codex plan card: plan content but NO plan file (the plan is the final message).
  const isCodexPlan = action === 'exit' && !planFilename && !!planContent

  // Notify parent about the fetched plan (for PlanPanel)
  useEffect(() => {
    if (action === 'exit' && planFilename && planContent) {
      onPlanFetched?.(planFilename, planContent)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [action, planFilename])

  if (action === 'enter') {
    return (
      <div className="my-2 p-3 rounded-lg border border-brand/20 bg-brand-50 text-sm text-brand flex items-center gap-2">
        <span className="inline-block w-2 h-2 rounded-full bg-brand animate-pulse" />
        Entered plan mode (read-only)
      </div>
    )
  }

  // Exit — show plan content + action buttons
  const displayName = planFilename || 'Plan'
  // A superseded card (a later plan turn replaced it) is inert — content only.
  const showActions = !superseded && (planFilename || isCodexPlan)

  return (
    <div className="my-2 rounded-lg border border-brand/20 bg-brand-50 overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center justify-between px-3 py-2 cursor-pointer hover:bg-brand-100 transition-colors"
        onClick={() => setCollapsed(!collapsed)}
      >
        <p className="text-sm font-medium text-brand flex items-center gap-2">
          <span>{collapsed ? '\u25B6' : '\u25BC'}</span>
          Plan: {displayName}
        </p>
        {collapsed && (
          <span className="text-xs text-brand/60">Click to expand</span>
        )}
      </div>

      {/* Plan content */}
      {!collapsed && (
        <>
          {planContent ? (
            <div className="px-4 py-3 border-t border-brand/20 bg-white dark:bg-p-surface max-h-96 overflow-y-auto">
              <div className="text-sm prose prose-sm max-w-none prose-headings:text-brand prose-li:text-gray-800 dark:prose-li:text-gray-200">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{planContent}</ReactMarkdown>
              </div>
            </div>
          ) : (
            <div className="px-4 py-3 border-t border-brand/20 text-sm text-brand/60">
              No plan content available
            </div>
          )}

          {/* Action buttons */}
          {showActions && (
            <div className="flex flex-wrap gap-2 px-4 py-3 border-t border-brand/20 bg-brand-50">
              {isCodexPlan ? (
                onImplementCodex && (
                  <>
                    <button
                      onClick={() => onImplementCodex('acceptEdits')}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium text-white bg-brand hover:bg-brand-hover transition-colors"
                    >
                      Start Implementation
                    </button>
                    <button
                      onClick={() => onImplementCodex('dontAsk')}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium text-brand bg-brand-100 hover:bg-brand-surface transition-colors"
                    >
                      Full Permissions
                    </button>
                  </>
                )
              ) : onImplement && (
                <>
                  <button
                    onClick={() => onImplement(planFilename, 'acceptEdits')}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium text-white bg-brand hover:bg-brand-hover transition-colors"
                  >
                    Start Implementation
                  </button>
                  <button
                    onClick={() => onImplement(planFilename, 'dontAsk')}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium text-brand bg-brand-100 hover:bg-brand-surface transition-colors"
                  >
                    Full Permissions
                  </button>
                </>
              )}
              {onSendMessage && (
                <>
                  <button
                    onClick={() => onSendMessage('Please modify the plan based on my feedback: ')}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium text-brand bg-white dark:bg-p-surface border border-brand/30 hover:bg-brand-50 transition-colors"
                  >
                    Edit Plan
                  </button>
                  <button
                    onClick={() => onSendMessage("Let's move on without implementing this plan.")}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium text-gray-600 dark:text-gray-300 bg-white dark:bg-p-surface border border-gray-300 dark:border-p-border-light hover:bg-gray-50 dark:hover:bg-p-surface-hover transition-colors"
                  >
                    Reject
                  </button>
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
