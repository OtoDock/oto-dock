import { useState } from 'react'
import MarkdownContent from '../MarkdownContent'
import Dialog from '../../ui/Dialog'
import { GLASS_FILL, GLASS_HOVER } from '../../ui/glass'
import { DIALOG_PANEL } from '../../ui/classes'

export interface SessionPlan {
  filename: string
  content: string
  status: 'pending' | 'implemented' | 'rejected'
}

interface Props {
  plans: SessionPlan[]
}

const STATUS_ICON: Record<string, { icon: string; color: string }> = {
  pending: { icon: '\u23F3', color: 'text-status-warning-fg' },
  implemented: { icon: '\u2713', color: 'text-status-success-fg' },
  rejected: { icon: '\u2717', color: 'text-status-danger-fg' },
}

// The toggle's edge says the plan's status.
const STATUS_BORDER: Record<string, string> = {
  pending: 'border-status-warning-border',
  implemented: 'border-status-success-border',
  rejected: 'border-subtle',
}

export default function PlanPanel({ plans }: Props) {
  const [openPlan, setOpenPlan] = useState<string | null>(null)

  if (plans.length === 0) return null

  const activePlan = openPlan ? plans.find(p => p.filename === openPlan) : null

  return (
    <>
      {/* Pinned plan notes */}
      <div className="flex flex-col gap-2 items-end">
        {plans.map((plan) => {
          const status = STATUS_ICON[plan.status] || STATUS_ICON.pending
          const borderColor = STATUS_BORDER[plan.status] || STATUS_BORDER.pending
          return (
            <button
              key={plan.filename}
              onClick={() => setOpenPlan(openPlan === plan.filename ? null : plan.filename)}
              title={`Plan: ${plan.status}`}
              className={`relative flex size-control-md items-center justify-center rounded-xl ${GLASS_FILL} ${GLASS_HOVER}
                         border ${borderColor} transition-colors duration-fast`}
            >
              {/* Notepad icon */}
              <svg className="w-5 h-5 text-secondary" viewBox="0 0 24 24" fill="none" stroke="currentColor">
                <rect x="5" y="3" width="14" height="18" rx="2" strokeWidth={1.5} />
                <path d="M9 3v2M15 3v2" strokeWidth={1.5} strokeLinecap="round" />
                <path d="M9 10h6M9 13h6M9 16h4" strokeWidth={1.3} strokeLinecap="round" />
              </svg>
              {/* Status badge — bottom-right corner */}
              <span className={`absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 rounded-full bg-surface-card border border-current flex items-center justify-center text-3xs font-bold ${status.color}`}>
                {plan.status === 'implemented' ? '\u2713' : plan.status === 'rejected' ? '\u2717' : '\u25CF'}
              </span>
            </button>
          )
        })}
      </div>

      {/* Plan viewer modal */}
      {activePlan && (
        <Dialog
          onClose={() => setOpenPlan(null)}
          placement="top"
          aria-label="Plan"
          panelClassName={`${DIALOG_PANEL} max-w-3xl max-h-[80vh] flex flex-col overflow-hidden`}
        >
            {/* Header */}
            <div className="flex items-center justify-between px-5 py-3 border-b border-subtle">
              <div className="flex items-center gap-2.5">
                <svg className="w-5 h-5 text-accent-agent-fg" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                <div>
                  <p className="text-sm font-semibold text-primary">Plan</p>
                  <p className={`text-xs ${STATUS_ICON[activePlan.status]?.color || 'text-tertiary'}`}>
                    {activePlan.status.charAt(0).toUpperCase() + activePlan.status.slice(1)}
                  </p>
                </div>
              </div>
              <button
                type="button"
                aria-label="Close"
                onClick={() => setOpenPlan(null)}
                className="w-8 h-8 rounded-control hover:bg-surface-hover flex items-center justify-center text-secondary hover:text-primary transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto px-6 py-4">
              {activePlan.content ? (
                <MarkdownContent>
                  {activePlan.content}
                </MarkdownContent>
              ) : (
                <p className="text-sm text-tertiary py-8 text-center">No plan content available</p>
              )}
            </div>
        </Dialog>
      )}
    </>
  )
}
