import { useState, useEffect, useRef } from 'react'
import MarkdownContent from '../MarkdownContent'
import { pushEscHandler } from '../../../lib/escStack'

export interface SessionPlan {
  filename: string
  content: string
  status: 'pending' | 'implemented' | 'rejected'
}

interface Props {
  plans: SessionPlan[]
}

const STATUS_ICON: Record<string, { icon: string; color: string }> = {
  pending: { icon: '\u23F3', color: 'text-[#b8860b]' },
  implemented: { icon: '\u2713', color: 'text-p-success' },
  rejected: { icon: '\u2717', color: 'text-p-accent-red' },
}

const STATUS_BORDER: Record<string, string> = {
  pending: 'border-[#f4b206]/40',
  implemented: 'border-p-success/30',
  rejected: 'border-p-border-light',
}

export default function PlanPanel({ plans }: Props) {
  const [openPlan, setOpenPlan] = useState<string | null>(null)
  const modalRef = useRef<HTMLDivElement>(null)

  // Close on Escape (precedence stack — only topmost panel handles each press)
  useEffect(() => {
    if (!openPlan) return
    return pushEscHandler(() => setOpenPlan(null))
  }, [openPlan])

  // Close on click outside
  useEffect(() => {
    if (!openPlan) return
    const handler = (e: MouseEvent) => {
      if (modalRef.current && !modalRef.current.contains(e.target as Node)) {
        setOpenPlan(null)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [openPlan])

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
              className={`relative w-10 h-10 rounded-xl bg-white/80 dark:bg-gray-900/80 backdrop-blur-xs border ${borderColor} shadow-xs
                         hover:shadow-md hover:bg-white dark:hover:bg-p-surface transition-all flex items-center justify-center`}
            >
              {/* Notepad icon */}
              <svg className="w-5 h-5 text-p-text-secondary" viewBox="0 0 24 24" fill="none" stroke="currentColor">
                <rect x="5" y="3" width="14" height="18" rx="2" strokeWidth={1.5} />
                <path d="M9 3v2M15 3v2" strokeWidth={1.5} strokeLinecap="round" />
                <path d="M9 10h6M9 13h6M9 16h4" strokeWidth={1.3} strokeLinecap="round" />
              </svg>
              {/* Status badge — bottom-right corner */}
              <span className={`absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 rounded-full bg-white dark:bg-p-surface border border-current flex items-center justify-center text-[8px] font-bold ${status.color}`}>
                {plan.status === 'implemented' ? '\u2713' : plan.status === 'rejected' ? '\u2717' : '\u25CF'}
              </span>
            </button>
          )
        })}
      </div>

      {/* Plan viewer modal */}
      {activePlan && (
        <div className="fixed inset-0 z-50 flex items-start justify-center pt-16 px-4">
          {/* Backdrop */}
          <div className="absolute inset-0 bg-black/30 backdrop-blur-xs" />

          {/* Modal */}
          <div
            ref={modalRef}
            className="relative w-full max-w-3xl max-h-[80vh] bg-white dark:bg-p-surface rounded-2xl shadow-2xl border border-p-border-light flex flex-col overflow-hidden"
          >
            {/* Header */}
            <div className="flex items-center justify-between px-5 py-3 border-b border-p-border-light bg-p-bg/50">
              <div className="flex items-center gap-2.5">
                <svg className="w-5 h-5 text-brand" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                <div>
                  <p className="text-sm font-semibold text-p-text">Plan</p>
                  <p className={`text-xs ${STATUS_ICON[activePlan.status]?.color || 'text-p-text-light'}`}>
                    {activePlan.status.charAt(0).toUpperCase() + activePlan.status.slice(1)}
                  </p>
                </div>
              </div>
              <button
                onClick={() => setOpenPlan(null)}
                className="w-8 h-8 rounded-lg bg-p-surface hover:bg-p-border flex items-center justify-center text-p-text-secondary hover:text-p-text transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto px-6 py-4">
              {activePlan.content ? (
                <MarkdownContent className="prose-headings:text-brand">
                  {activePlan.content}
                </MarkdownContent>
              ) : (
                <p className="text-sm text-p-text-light py-8 text-center">No plan content available</p>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
