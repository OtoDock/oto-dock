import { useState } from 'react'
import { PAYLOAD, PAYLOAD_KEYS, ROLE, toolPayload, toolRole, payloadText, payloadValue } from '../../lib/tools/roles'

interface Props {
  name: string
  summary?: string
  /** `failed` is set when the user stopped generation mid-call; rendered
   * with a red X instead of the green check so it's clear the tool didn't
   * complete normally. */
  status: 'running' | 'done' | 'failed'
  toolInput?: any
  toolResult?: string
  resultSummary?: string
}

// The cards key on what a call CARRIES (lib/tools/roles: its payload kind and
// the input keys that kind is read from) and on its role — never on the tool's
// name, so an engine that maps its own tools into the platform's vocabulary
// renders like every other.

/** A patch's file paths: the app-server item's `changes[].path`, else the
 *  `*** Add|Update|Delete File:` headers of the patch text. */
function patchPaths(input: any): string[] {
  const changes = Array.isArray(input?.changes) ? input.changes : []
  const fromItem = changes.map((c: any) => (c && typeof c.path === 'string' ? c.path : '')).filter(Boolean)
  if (fromItem.length) return fromItem
  const text = payloadText('apply_patch', input)
  const out: string[] = []
  for (const m of text.matchAll(/^\*\*\* (?:Add|Update|Delete) File: (.+)$/gm)) out.push(m[1].trim())
  return out
}

/** A patch's body: the diffs of the item's changes, else the patch text. */
function patchBody(input: any): string {
  const changes = Array.isArray(input?.changes) ? input.changes : []
  const diffs = changes
    .map((c: any) => {
      const kind = (c?.kind && typeof c.kind === 'object' && c.kind.type) || 'update'
      const head = `${kind} ${c?.path || ''}`
      return c?.diff ? `${head}\n${c.diff}` : head
    })
    .filter(Boolean)
  return diffs.length ? diffs.join('\n') : payloadText('apply_patch', input)
}

/** A skill or workflow's name: the input's `name`, else the meta block of an
 *  inline workflow script ("Workflow review-changes"). */
function nameOf(name: string, input: any): string {
  const direct = payloadText(name, input)
  if (direct) return direct
  return typeof input?.script === 'string'
    ? (input.script.match(/name:\s*['"]([^'"]+)['"]/)?.[1] ?? '')
    : ''
}

export function getToolDetail(name: string, summary: string | undefined, toolInput: any): string {
  // A shell: the model-written description says WHAT the command does —
  // that's the collapsed title (the command itself is the expanded detail).
  // Wins over `summary`, which older rows carry as the raw command. Generous
  // cap: collapsed rendering clips to one line via CSS, and the expanded
  // pill un-truncates this same text (see ToolActivity), so keep the full
  // sentence.
  const role = toolRole(name)
  if (role === ROLE.SHELL && toolInput?.description) {
    return truncate(toolInput.description, 300)
  }
  if (summary) return summary
  if (!toolInput) return ''
  switch (toolPayload(name)) {
    case PAYLOAD.COMMAND:
      return truncate(payloadText(name, toolInput), 120)
    case PAYLOAD.FILE_PATH:
      return payloadText(name, toolInput)
    case PAYLOAD.PATCH: {
      const paths = patchPaths(toolInput)
      return paths.length ? paths.map((p) => p.split('/').pop() || p).join(', ') : ''
    }
    case PAYLOAD.SEARCH_PATH:
      // A content search reads "pattern in path"; a glob is its pattern.
      return role === ROLE.SEARCH
        ? [toolInput.pattern, payloadText(name, toolInput)].filter(Boolean).join(' in ')
        : toolInput.pattern || ''
    case PAYLOAD.QUERY:
      return payloadText(name, toolInput)
    case PAYLOAD.URL:
      return truncate(payloadText(name, toolInput), 100)
    case PAYLOAD.DESCRIPTION:
      return payloadText(name, toolInput)
    case PAYLOAD.NAME:
      return nameOf(name, toolInput)
    default:
      return ''
  }
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '...' : s
}

function truncateLines(s: string, maxLines: number): { text: string; truncated: number } {
  const lines = s.split('\n')
  if (lines.length <= maxLines) return { text: s, truncated: 0 }
  return { text: lines.slice(0, maxLines).join('\n'), truncated: lines.length - maxLines }
}

function Pre({ children, tone }: { children: React.ReactNode; tone?: 'removed' | 'added' }) {
  const cls = tone === 'removed'
    ? 'bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-300 max-h-60'
    : tone === 'added'
      ? 'bg-green-50 dark:bg-green-900/20 text-green-800 dark:text-green-300 max-h-60'
      : 'bg-p-surface dark:bg-p-bg max-h-80'
  return <pre className={`whitespace-pre-wrap rounded-sm px-2 py-1.5 overflow-y-auto ${cls}`}>{children}</pre>
}

/** The input carries keys a branch does not render (a notebook cell, a
 *  multi-edit's edits, a workflow's script): the JSON body shows them all. */
function carriesMore(input: any, shown: string[]): boolean {
  return Object.keys(input).some((k) => !shown.includes(k))
}

function ToolDetail({ name, toolInput }: { name: string; toolInput: any }) {
  if (!toolInput) return null
  const payload = toolPayload(name)
  const role = toolRole(name)

  if (payload === PAYLOAD.FILE_PATH) {
    const fp = payloadText(name, toolInput)
    // An edit carries the old and new text; a write its content; a read
    // its window; a delete (or an edit without text) the path alone.
    if (typeof toolInput.old_string === 'string' || typeof toolInput.new_string === 'string') {
      const oldStr = toolInput.old_string || ''
      const newStr = toolInput.new_string || ''
      return (
        <div className="space-y-1.5">
          {fp && <div className="text-p-text-secondary font-mono">{fp}</div>}
          {oldStr && (
            <Pre tone="removed">
              {oldStr.split('\n').map((line: string, i: number) => (
                <span key={i}>{`- ${line}\n`}</span>
              ))}
            </Pre>
          )}
          {newStr && (
            <Pre tone="added">
              {newStr.split('\n').map((line: string, i: number) => (
                <span key={i}>{`+ ${line}\n`}</span>
              ))}
            </Pre>
          )}
        </div>
      )
    }
    if (role === ROLE.WRITE && typeof toolInput.content === 'string') {
      const { text, truncated } = truncateLines(toolInput.content, 200)
      return (
        <div className="space-y-1.5">
          {fp && <div className="text-p-text-secondary font-mono">{fp}</div>}
          <Pre>
            {text}
            {truncated > 0 && <span className="text-p-text-light">{`\n... (${truncated} more lines)`}</span>}
          </Pre>
        </div>
      )
    }
    if (role === ROLE.READ) {
      return (
        <div className="space-y-0.5">
          <div className="font-mono">{fp}</div>
          {(toolInput.offset || toolInput.limit) && (
            <div className="text-p-text-light">
              {toolInput.offset ? `offset: ${toolInput.offset}` : ''}
              {toolInput.offset && toolInput.limit ? ' · ' : ''}
              {toolInput.limit ? `limit: ${toolInput.limit}` : ''}
            </div>
          )}
        </div>
      )
    }
    if (!carriesMore(toolInput, PAYLOAD_KEYS[PAYLOAD.FILE_PATH])) {
      return <div className="font-mono">{fp}</div>
    }
  }

  if (payload === PAYLOAD.PATCH) {
    // A file change as a file change: its paths, then the diffs (the
    // app-server item) or the patch text (the rollout, the hook wire).
    const paths = patchPaths(toolInput)
    const { text, truncated } = truncateLines(patchBody(toolInput), 200)
    return (
      <div className="space-y-1.5">
        {paths.map((p) => <div key={p} className="text-p-text-secondary font-mono">{p}</div>)}
        <Pre>
          {text}
          {truncated > 0 && <span className="text-p-text-light">{`\n... (${truncated} more lines)`}</span>}
        </Pre>
      </div>
    )
  }

  if (payload === PAYLOAD.COMMAND) {
    // The description is the collapsed pill title (getToolDetail) — the
    // expanded body is the command itself.
    return <Pre>{payloadText(name, toolInput)}</Pre>
  }

  if (payload === PAYLOAD.SEARCH_PATH) {
    return (
      <div className="space-y-0.5">
        {toolInput.pattern && <div className="font-mono">pattern: {toolInput.pattern}</div>}
        {toolInput.path && <div className="font-mono">path: {toolInput.path}</div>}
        {toolInput.type && <div className="text-p-text-light">type: {toolInput.type}</div>}
        {toolInput.output_mode && <div className="text-p-text-light">mode: {toolInput.output_mode}</div>}
      </div>
    )
  }

  if (payload === PAYLOAD.NAME && !carriesMore(toolInput, PAYLOAD_KEYS[PAYLOAD.NAME])) {
    return <div className="font-mono">{nameOf(name, toolInput)}</div>
  }

  if (payload === PAYLOAD.QUERY) {
    return (
      <div className="space-y-0.5">
        {toolInput.query && <div className="font-mono">query: {toolInput.query}</div>}
        {toolInput.max_results && <div className="text-p-text-light">max results: {toolInput.max_results}</div>}
      </div>
    )
  }

  if (payload === PAYLOAD.TODOS) {
    const todos = payloadValue(name, toolInput)
    const list = Array.isArray(todos) ? todos : []
    return (
      <div className="space-y-0.5">
        {list.map((t: any, i: number) => (
          <div key={i} className="flex items-center gap-2">
            {t.status === 'completed' ? (
              <span className="text-p-success text-[10px]">&#10003;</span>
            ) : (
              <span className="inline-block w-2.5 h-2.5 rounded-full border border-p-text-light" />
            )}
            <span className={t.status === 'completed' ? 'line-through text-p-text-light' : ''}>
              {t.content}
            </span>
          </div>
        ))}
      </div>
    )
  }

  // Default: formatted JSON for MCP tools and others
  const json = JSON.stringify(toolInput, null, 2)
  const { text, truncated } = truncateLines(json, 200)
  return (
    <Pre>
      {text}
      {truncated > 0 && <span className="text-p-text-light">{`\n... (${truncated} more lines)`}</span>}
    </Pre>
  )
}

export default function ToolActivity({ name, summary, status, toolInput, toolResult, resultSummary }: Props) {
  const [expanded, setExpanded] = useState(false)
  const detail = getToolDetail(name, summary, toolInput)
  const expandable = !!toolInput || !!toolResult

  // Expanding un-truncates the collapsed title when it's a shell description:
  // the full sentence wraps in place, reading above the command in the body.
  // The no-description fallback (title = the raw command, e.g. Codex) stays
  // clipped — the body already shows the command verbatim.
  const wrapDetail = expanded && toolRole(name) === ROLE.SHELL && !!toolInput?.description

  // Show result summary inline (e.g., "15 lines", "3 results", "ok")
  const inlineSummary = resultSummary || ''

  return (
    <div className="my-1.5 rounded-lg bg-p-surface/50 dark:border dark:border-gray-700/50 text-xs overflow-hidden">
      <div
        className={`flex items-start gap-2 py-1 px-2 text-p-text-secondary ${expandable ? 'cursor-pointer hover:bg-p-surface/80 dark:hover:bg-gray-700/30' : ''}`}
        onClick={expandable ? () => setExpanded(!expanded) : undefined}
      >
        {expandable && (
          <span className={`mt-0.5 shrink-0 text-[10px] text-p-text-light transform transition-transform ${expanded ? 'rotate-90' : ''}`}>
            &#9654;
          </span>
        )}
        <span className="mt-0.5 shrink-0">
          {status === 'running' ? (
            <span className="inline-block w-3 h-3 border-2 border-p-text-light border-t-transparent rounded-full animate-spin" />
          ) : status === 'failed' ? (
            <span className="text-p-error" title="Stopped before completion">&#10007;</span>
          ) : (
            <span className="text-p-success">&#10003;</span>
          )}
        </span>
        <span className="font-medium text-p-text shrink-0">{name}</span>
        {detail && (
          <span
            className={`text-p-text-light font-mono text-[11px] ${wrapDetail ? 'whitespace-normal break-words min-w-0' : 'truncate'}`}
            title={detail}
          >
            {detail}
          </span>
        )}
        {inlineSummary && status === 'done' && (
          <span className="text-p-text-light text-[11px] ml-auto shrink-0">
            {inlineSummary}
          </span>
        )}
      </div>
      {expanded && (toolInput || toolResult) && (
        <div className="border-t border-p-border-light px-3 py-2 text-xs font-mono text-p-text space-y-2">
          {toolInput && <ToolDetail name={name} toolInput={toolInput} />}
          {toolResult && (
            <div>
              <div className="text-p-text-secondary font-sans font-medium mb-1">Output</div>
              <pre className="whitespace-pre-wrap bg-p-surface dark:bg-p-bg rounded-sm px-2 py-1.5 max-h-80 overflow-y-auto text-p-text">
                {toolResult}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
