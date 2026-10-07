import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { resolveChatPath, type ResolvedChatPath } from '../../api/chats'
import type { DelegateResultFile, DelegateResultSkipped } from '../../api/wireEvents'
import { scopeWorkspace, userOf } from '../../lib/layout/tree'
import { DELEGATE_RESULT, RUN_STATUS, type DelegateBlockStatus } from '../../lib/status/run'
import { useChatFileContext } from './ChatFileContext'
import ChatFilePreview from './ChatFilePreview'

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
            {status === RUN_STATUS.RUNNING ? (
              <span className="inline-block w-3 h-3 border-2 border-p-accent-teal border-t-transparent rounded-full animate-spin" />
            ) : status === DELEGATE_RESULT.COMPLETED ? (
              <span className="text-p-accent-teal">&#10003;</span>
            ) : status === DELEGATE_RESULT.CANCELLED ? (
              <span className="text-p-text-light">&#10005;</span>
            ) : status === DELEGATE_RESULT.USER_INTERRUPTED ? (
              <span className="text-amber-500" title="The user stopped or redirected this lane">&#9208;</span>
            ) : (
              <span className="text-red-500">&#10007;</span>
            )}
          </span>
          {agentBadge('hidden sm:inline-block')}
          <span className="truncate font-medium">{taskName}</span>
          {status === DELEGATE_RESULT.USER_INTERRUPTED && (
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

/** A landed path as the delegating chat's workspace names it: the scope
 * workspace prefix (`workspace/`, `users/<u>/workspace/`) stripped, so the
 * label matches the callback note (`inbox/<worker>/report.md`). */
export function resultFileLabel(path: string): string {
  const prefix = `${scopeWorkspace(userOf(path))}/`
  return path.startsWith(prefix) ? path.slice(prefix.length) : path
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
export function DelegateResultFiles({ files, skipped }: {
  files: DelegateResultFile[]
  skipped: DelegateResultSkipped[]
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
    let resolved: ResolvedChatPath | null = null
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

  return (
    <div className="my-1.5 rounded-lg bg-[#0d9488]/5 text-xs overflow-hidden" data-testid="delegate-files">
      <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-p-accent-teal/80">
        Files from the worker
      </div>
      <ul className="px-2 pb-1.5 space-y-0.5">
        {files.map((f) => (
          <li key={f.path} className="flex items-center gap-2 min-w-0">
            <span aria-hidden="true">📄</span>
            <span className="truncate font-mono text-p-text" title={f.path}>{resultFileLabel(f.path)}</span>
            <span className="shrink-0 text-p-text-light">{sizeText(f.bytes)}</span>
            {ctx && (
              <button
                type="button"
                onClick={() => open(f.path)}
                disabled={busy === f.path}
                title={notFound === f.path ? 'File not found in this chat\'s workspace' : 'Open file preview'}
                className="shrink-0 ml-auto px-1.5 py-0.5 rounded-sm text-[10px] font-medium text-p-accent-teal hover:bg-p-accent-teal/10 transition-colors disabled:opacity-60"
              >
                {notFound === f.path ? 'not found' : 'Open ↗'}
              </button>
            )}
          </li>
        ))}
        {skipped.map((s, i) => (
          <li key={`skip-${i}`} className="flex items-center gap-2 min-w-0 text-p-text-light">
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
