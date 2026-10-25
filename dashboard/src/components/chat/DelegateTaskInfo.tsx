import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { resolveChatPath, type ResolvedChatPath } from '../../api/chats'
import type { DelegateResultFile, DelegateResultSkipped } from '../../api/wireEvents'
import { DELEGATE_RESULT, RUN_STATUS, type DelegateBlockStatus } from '../../lib/status/run'

import ChatFilePreview from './ChatFilePreview'
import { resultFileLabel } from '../../lib/chat/delegateFiles'
import { delegatePreviewText } from '../../lib/messageBlocks'
import { useChatFileContext } from '../../lib/chat/chatFileContext'

interface Props {
  taskName: string
  agent: string
  promptPreview: string
  status: DelegateBlockStatus
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
  // The worker's task, not the platform's wrapper around it (the full
  // prompt when the spawn carried it; older rows have the 100-char head).
  const shownPreview = fullPrompt
    ? delegatePreviewText('prompt', [{ type: 'text', content: fullPrompt }])
    : promptPreview

  // Rendered twice: inline on sm+, on its own row below the name on narrow
  // screens — shrink-0 so flex can never crush it into a vertical strip.
  const agentBadge = (visibility: string) => (
    <span className={`${visibility} shrink-0 max-w-40 truncate px-1.5 py-0.5 rounded-sm text-3xs font-medium bg-surface-card text-accent-agent-fg`}>
      {agent}
    </span>
  )

  return (
    <div className="my-1.5 rounded-card bg-accent-agent-bg text-xs text-accent-agent-fg overflow-hidden">
      <div
        className={`py-1.5 px-2 ${expandable ? 'cursor-pointer hover:bg-surface-hover' : ''}`}
        onClick={expandable ? () => setExpanded(!expanded) : undefined}
      >
        <div className="flex items-center gap-2 min-w-0">
          {expandable && (
            <span className={`shrink-0 text-3xs transform transition-transform ${expanded ? 'rotate-90' : ''}`}>
              &#9654;
            </span>
          )}
          <span className="shrink-0">
            {status === RUN_STATUS.RUNNING ? (
              <span className="inline-block w-3 h-3 border-2 border-accent-agent-solid border-t-transparent rounded-full animate-spin" />
            ) : status === DELEGATE_RESULT.COMPLETED ? (
              <span className="text-accent-agent-fg">&#10003;</span>
            ) : status === DELEGATE_RESULT.CANCELLED ? (
              <span className="text-tertiary">&#10005;</span>
            ) : status === DELEGATE_RESULT.USER_INTERRUPTED ? (
              <span className="text-status-warning-fg" title="The user stopped or redirected this lane">&#9208;</span>
            ) : (
              <span className="text-status-danger-fg">&#10007;</span>
            )}
          </span>
          {agentBadge('hidden sm:inline-block')}
          <span className="truncate font-medium">{taskName}</span>
          {status === DELEGATE_RESULT.USER_INTERRUPTED && (
            <span className="shrink-0 px-1.5 py-0.5 rounded-sm text-3xs font-medium bg-status-warning-bg text-status-warning-fg">
              Interrupted by user
            </span>
          )}
          {shownPreview && (
            <span className="hidden sm:inline flex-1 basis-0 truncate text-accent-agent-fg ml-1">{shownPreview}</span>
          )}
          {workerChatId && (
            <Link
              to={`/chat/${agent}/${workerChatId}`}
              onClick={(e) => e.stopPropagation()}
              className="shrink-0 ml-auto px-1.5 py-0.5 rounded-sm text-3xs font-medium
                         text-accent-agent-fg hover:bg-surface-hover transition-colors"
              title="Open the worker lane chat"
            >
              Open lane ↗
            </Link>
          )}
        </div>
        <div className="flex sm:hidden items-center gap-2 min-w-0 mt-1">
          {agentBadge('')}
          {shownPreview && (
            <span className="truncate text-accent-agent-fg">{shownPreview}</span>
          )}
        </div>
      </div>
      {expanded && expandable && (
        <div className="border-t border-subtle px-3 py-2 text-xs font-mono text-primary">
          <div className="text-secondary font-sans font-medium mb-1">Prompt</div>
          <pre className="whitespace-pre-wrap bg-surface-sunken rounded-sm px-2 py-1.5 max-h-80 overflow-y-auto">
            {fullPrompt}
          </pre>
        </div>
      )}
    </div>
  )
}

function sizeText(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

const NOT_FOUND_REVERT_MS = 2500

/**
 * The files a delegated worker attached to its result, rendered inside the
 * worker's response bubble. Each landed file opens through the DELEGATING
 * chat's `resolve-path` (`useChatFileContext()`, this chat's id and agent),
 * never through the bubble's agent (the worker's slug): the files live in
 * this chat's tree. Without a chat context (a chat with no id yet) the rows
 * are inert text. Skipped entries show muted with the proxy's reason.
 */
export function DelegateResultFiles({ files, skipped, compact = false }: {
  files: DelegateResultFile[]
  skipped: DelegateResultSkipped[]
  /** One line of chips (a collapsed delegate card): the landed files only,
   * each opening like the list's Open. */
  compact?: boolean
}) {
  const ctx = useChatFileContext()
  const [preview, setPreview] = useState<ResolvedChatPath | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [notFound, setNotFound] = useState<string | null>(null)
  const revertTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (revertTimer.current) clearTimeout(revertTimer.current)
  }, [])

  const open = async (path: string) => {
    if (!ctx || busy) return
    if (revertTimer.current) clearTimeout(revertTimer.current)
    setNotFound(null)
    setBusy(path)
    let resolved: ResolvedChatPath | null
    try {
      resolved = await resolveChatPath(ctx.chatId, path)
    } catch {
      resolved = null
    }
    setBusy(null)
    if (resolved) {
      setPreview(resolved)
      return
    }
    setNotFound(path)
    revertTimer.current = setTimeout(() => setNotFound(null), NOT_FOUND_REVERT_MS)
  }

  if (compact) {
    if (files.length === 0) return null
    return (
      <div className="flex flex-wrap gap-1.5" data-testid="delegate-file-chips">
        {files.map((f) => (
          <button
            key={f.path}
            type="button"
            onClick={() => open(f.path)}
            disabled={!ctx || busy === f.path}
            title={notFound === f.path ? 'File not found in this chat\'s workspace' : f.path}
            className="inline-flex items-center gap-1 max-w-full rounded-control px-2 py-0.5 text-2xs font-medium
                       bg-surface-card text-accent-agent-fg hover:underline disabled:opacity-60
                       focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring"
          >
            <span aria-hidden="true">📄</span>
            <span className="truncate font-mono">{notFound === f.path ? 'not found' : resultFileLabel(f.path)}</span>
          </button>
        ))}
        {preview && <ChatFilePreview resolved={preview} onClose={() => setPreview(null)} />}
      </div>
    )
  }

  return (
    <div className="my-1.5 rounded-card bg-accent-agent-bg text-xs overflow-hidden" data-testid="delegate-files">
      <div className="px-2 py-1 text-3xs font-medium uppercase tracking-wide text-accent-agent-fg">
        Files from the worker
      </div>
      <ul className="px-2 pb-1.5 space-y-0.5">
        {files.map((f) => (
          <li key={f.path} className="flex items-center gap-2 min-w-0">
            <span aria-hidden="true">📄</span>
            <span className="truncate font-mono text-primary" title={f.path}>{resultFileLabel(f.path)}</span>
            <span className="shrink-0 text-tertiary">{sizeText(f.bytes)}</span>
            {ctx && (
              <button
                type="button"
                onClick={() => open(f.path)}
                disabled={busy === f.path}
                title={notFound === f.path ? 'File not found in this chat\'s workspace' : 'Open file preview'}
                className="shrink-0 ml-auto px-1.5 py-0.5 rounded-sm text-3xs font-medium text-accent-agent-fg hover:bg-surface-hover transition-colors disabled:opacity-60"
              >
                {notFound === f.path ? 'Not found' : 'Open ↗'}
              </button>
            )}
          </li>
        ))}
        {skipped.map((s, i) => (
          <li key={`skip-${i}`} className="flex items-center gap-2 min-w-0 text-tertiary">
            <span aria-hidden="true">⊘</span>
            <span className="truncate font-mono" title={s.path}>{s.path}</span>
            <span className="shrink-0 truncate">{s.reason}</span>
          </li>
        ))}
      </ul>
      {preview && <ChatFilePreview resolved={preview} onClose={() => setPreview(null)} />}
    </div>
  )
}
