import { useState } from 'react'
import { Link } from 'react-router-dom'

interface Props {
  taskName: string
  agent: string
  promptPreview: string
  status: 'running' | 'completed' | 'failed' | 'cancelled' | 'user_interrupted'
  /** Full delegated prompt — enables the expanded view (older rows only
   * carry the 100-char preview). */
  prompt?: string
  /** Chat-surface lane's worker chat — renders the open-lane link. */
  workerChatId?: string
}

export default function DelegateTaskInfo({ taskName, agent, promptPreview, status, prompt, workerChatId }: Props) {
  const [expanded, setExpanded] = useState(false)
  const fullPrompt = prompt || ''
  const expandable = !!fullPrompt

  // Rendered twice: inline on sm+, on its own row below the name on narrow
  // screens — shrink-0 so flex can never crush it into a vertical strip.
  const agentBadge = (visibility: string) => (
    <span className={`${visibility} shrink-0 max-w-40 truncate px-1.5 py-0.5 rounded-sm text-[10px] font-medium bg-p-accent-purple/10 text-p-accent-purple`}>
      {agent}
    </span>
  )

  return (
    <div className="my-1.5 rounded-lg bg-[#0d9488]/5 text-xs text-p-accent-teal overflow-hidden">
      <div
        className={`py-1.5 px-2 ${expandable ? 'cursor-pointer hover:bg-black/5 dark:hover:bg-white/5' : ''}`}
        onClick={expandable ? () => setExpanded(!expanded) : undefined}
      >
        <div className="flex items-center gap-2 min-w-0">
          {expandable && (
            <span className={`shrink-0 text-[10px] transform transition-transform ${expanded ? 'rotate-90' : ''}`}>
              &#9654;
            </span>
          )}
          <span className="shrink-0">
            {status === 'running' ? (
              <span className="inline-block w-3 h-3 border-2 border-p-accent-teal border-t-transparent rounded-full animate-spin" />
            ) : status === 'completed' ? (
              <span className="text-p-accent-teal">&#10003;</span>
            ) : status === 'cancelled' ? (
              <span className="text-p-text-light">&#10005;</span>
            ) : status === 'user_interrupted' ? (
              <span className="text-amber-500" title="The user stopped or redirected this lane">&#9208;</span>
            ) : (
              <span className="text-red-500">&#10007;</span>
            )}
          </span>
          {agentBadge('hidden sm:inline-block')}
          <span className="truncate font-medium">{taskName}</span>
          {status === 'user_interrupted' && (
            <span className="shrink-0 px-1.5 py-0.5 rounded-sm text-[10px] font-medium bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
              interrupted by user
            </span>
          )}
          {promptPreview && (
            <span className="hidden sm:inline flex-1 basis-0 truncate text-p-accent-teal/70 ml-1">{promptPreview}</span>
          )}
          {workerChatId && (
            <Link
              to={`/chat/${agent}/${workerChatId}`}
              onClick={(e) => e.stopPropagation()}
              className="shrink-0 ml-auto px-1.5 py-0.5 rounded-sm text-[10px] font-medium
                         text-p-accent-teal hover:bg-p-accent-teal/10 transition-colors"
              title="Open the worker lane chat"
            >
              open lane ↗
            </Link>
          )}
        </div>
        <div className="flex sm:hidden items-center gap-2 min-w-0 mt-1">
          {agentBadge('')}
          {promptPreview && (
            <span className="truncate text-p-accent-teal/70">{promptPreview}</span>
          )}
        </div>
      </div>
      {expanded && expandable && (
        <div className="border-t border-black/10 dark:border-white/10 px-3 py-2 text-xs font-mono text-p-text">
          <div className="text-p-text-secondary font-sans font-medium mb-1">Prompt</div>
          <pre className="whitespace-pre-wrap bg-p-surface dark:bg-p-bg rounded-sm px-2 py-1.5 max-h-80 overflow-y-auto">
            {fullPrompt}
          </pre>
        </div>
      )}
    </div>
  )
}
