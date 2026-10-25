import { useState, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { inputText } from '../../../lib/tools/input'
import { AUTO_REVIEW_MODE, DONT_ASK_MODE } from '../../../lib/engines/modes'
import Button from '../../ui/Button'

interface Props {
  action: string
  toolInput?: unknown
  superseded?: boolean
  onImplement?: (planPath: string, mode: string) => void
  // Codex has no plan FILE (the plan is the turn's final message) — implement
  // switches the permission mode + sends the build turn instead of implement_plan.
  onImplementCodex?: (mode: string) => void
  onSendMessage?: (text: string) => void
  onPlanFetched?: (filename: string, content: string) => void
  // A live interactive terminal owns the approval: the TUI shows its own
  // "proceed?" dialog, so the card carries no Implement / Edit / Reject
  // buttons (a card send would only be held until the parked turn ends).
  readOnly?: boolean
}

export default function PlanView({ action, toolInput, superseded, onImplement, onImplementCodex, onSendMessage, onPlanFetched, readOnly }: Props) {
  const [collapsed, setCollapsed] = useState(false)

  // ExitPlanMode tool_input has: planFilePath, plan (content), allowedPrompts
  const planPath = inputText(toolInput, 'planFilePath') || inputText(toolInput, 'plan_file_path') || inputText(toolInput, 'file_path')
  const planFilename = planPath ? (planPath.split('/').pop() || planPath) : ''
  // Plan content comes directly from the tool_input — no API fetch needed
  const planContent = inputText(toolInput, 'plan')
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
      <div className="my-2 p-3 rounded-card border border-accent-agent-border bg-accent-agent-bg text-sm text-accent-agent-fg flex items-center gap-2">
        <span className="inline-block w-2 h-2 rounded-full bg-accent-agent-solid animate-pulse" />
        Entered plan mode, read only
      </div>
    )
  }

  // Exit — show plan content + action buttons
  const displayName = planFilename || 'Plan'
  // A superseded card (a later plan turn replaced it) is inert — content only.
  const showActions = !superseded && !readOnly && (planFilename || isCodexPlan)

  return (
    <div className="my-2 rounded-card bg-surface-card shadow-elevation-1 overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center justify-between px-3 py-2 cursor-pointer hover:bg-surface-hover transition-colors"
        onClick={() => setCollapsed(!collapsed)}
      >
        <p className="text-sm font-medium text-accent-agent-fg flex items-center gap-2">
          <span>{collapsed ? '\u25B6' : '\u25BC'}</span>
          Plan: {displayName}
        </p>
        {collapsed && (
          <span className="text-xs text-tertiary">Click to expand</span>
        )}
      </div>

      {/* Plan content */}
      {!collapsed && (
        <>
          {planContent ? (
            <div className="px-4 py-3 border-t border-subtle max-h-96 overflow-y-auto">
              <div className="text-sm prose prose-sm max-w-none prose-li:text-primary">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{planContent}</ReactMarkdown>
              </div>
            </div>
          ) : (
            <div className="px-4 py-3 border-t border-subtle text-sm text-tertiary">
              No plan content available
            </div>
          )}

          {readOnly && !superseded && (
            <p
              data-testid="plan-readonly"
              className="px-4 py-2 border-t border-subtle text-xs text-tertiary"
            >
              Approve or reject in the terminal. The agent is waiting there.
            </p>
          )}

          {/* Action buttons */}
          {showActions && (
            <div className="flex flex-wrap gap-2 px-4 py-3 border-t border-subtle">
              {isCodexPlan ? (
                onImplementCodex && (
                  <>
                    <Button
                      size="sm" variant="primary"
                      onClick={() => onImplementCodex(AUTO_REVIEW_MODE)}
                    >
                      Start Implementation
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => onImplementCodex(DONT_ASK_MODE)}
                    >
                      Full Permissions
                    </Button>
                  </>
                )
              ) : onImplement && (
                <>
                  <Button
                    size="sm" variant="primary"
                    onClick={() => onImplement(planFilename, AUTO_REVIEW_MODE)}
                  >
                    Start Implementation
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => onImplement(planFilename, DONT_ASK_MODE)}
                  >
                    Full Permissions
                  </Button>
                </>
              )}
              {onSendMessage && (
                <>
                  <Button
                    size="sm"
                    onClick={() => onSendMessage('Please modify the plan based on my feedback: ')}
                  >
                    Edit Plan
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => onSendMessage("Let's move on without implementing this plan.")}
                  >
                    Reject
                  </Button>
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
