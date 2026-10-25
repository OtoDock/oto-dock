import { useState } from 'react'
import { PAYLOAD, PAYLOAD_KEYS, ROLE, toolPayload, toolRole, payloadText, payloadValue } from '../../lib/tools/roles'
import { getToolDetail, nameOf, patchPaths } from '../../lib/tools/detail'
import { inputText } from '../../lib/tools/input'
import { isRecord } from '../../lib/json/guards'

interface Props {
  name: string
  summary?: string
  /** `failed` is set when the user stopped generation mid-call; rendered
   * with a red X instead of the green check so it's clear the tool didn't
   * complete normally. */
  status: 'running' | 'done' | 'failed'
  toolInput?: unknown
  toolResult?: string
  resultSummary?: string
}

// The cards key on what a call CARRIES (lib/tools/roles: its payload kind and
// the input keys that kind is read from) and on its role — never on the tool's
// name, so an engine that maps its own tools into the platform's vocabulary
// renders like every other.

/** A patch's body: the diffs of the item's changes, else the patch text. */
function patchBody(input: unknown): string {
  const changes: unknown[] = isRecord(input) && Array.isArray(input.changes) ? input.changes : []
  const diffs = changes
    .map((item) => {
      const c = isRecord(item) ? item : {}
      const kind = (isRecord(c.kind) && c.kind.type) || 'update'
      const head = `${kind} ${c.path || ''}`
      return c.diff ? `${head}\n${c.diff}` : head
    })
    .filter(Boolean)
  return diffs.length ? diffs.join('\n') : payloadText('apply_patch', input)
}

function truncateLines(s: string, maxLines: number): { text: string; truncated: number } {
  const lines = s.split('\n')
  if (lines.length <= maxLines) return { text: s, truncated: 0 }
  return { text: lines.slice(0, maxLines).join('\n'), truncated: lines.length - maxLines }
}

function Pre({ children, tone }: { children: React.ReactNode; tone?: 'removed' | 'added' }) {
  const cls = tone === 'removed'
    ? 'bg-status-danger-bg text-status-danger-fg max-h-60'
    : tone === 'added'
      ? 'bg-status-success-bg text-status-success-fg max-h-60'
      : 'bg-surface-sunken max-h-80'
  return <pre className={`whitespace-pre-wrap rounded-sm px-2 py-1.5 overflow-y-auto ${cls}`}>{children}</pre>
}

/** The input carries keys a branch does not render (a notebook cell, a
 *  multi-edit's edits, a workflow's script): the JSON body shows them all. */
function carriesMore(input: unknown, shown: string[]): boolean {
  return isRecord(input) && Object.keys(input).some((k) => !shown.includes(k))
}

/** An input value as the card prints it. */
function show(v: unknown): string {
  return v == null ? '' : String(v)
}

function ToolDetail({ name, toolInput }: { name: string; toolInput: unknown }) {
  if (!toolInput) return null
  const input = isRecord(toolInput) ? toolInput : {}
  const payload = toolPayload(name)
  const role = toolRole(name)

  if (payload === PAYLOAD.FILE_PATH) {
    const fp = payloadText(name, toolInput)
    // An edit carries the old and new text; a write its content; a read
    // its window; a delete (or an edit without text) the path alone.
    if (typeof input.old_string === 'string' || typeof input.new_string === 'string') {
      const oldStr = inputText(input, 'old_string')
      const newStr = inputText(input, 'new_string')
      return (
        <div className="space-y-1.5">
          {fp && <div className="text-secondary font-mono">{fp}</div>}
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
    const content = input.content
    if (role === ROLE.WRITE && typeof content === 'string') {
      const { text, truncated } = truncateLines(content, 200)
      return (
        <div className="space-y-1.5">
          {fp && <div className="text-secondary font-mono">{fp}</div>}
          <Pre>
            {text}
            {truncated > 0 && <span className="text-tertiary">{`\n... ${truncated} more lines`}</span>}
          </Pre>
        </div>
      )
    }
    if (role === ROLE.READ) {
      return (
        <div className="space-y-0.5">
          <div className="font-mono">{fp}</div>
          {input.offset || input.limit ? (
            <div className="text-tertiary">
              {input.offset ? `offset: ${show(input.offset)}` : ''}
              {input.offset && input.limit ? ' · ' : ''}
              {input.limit ? `limit: ${show(input.limit)}` : ''}
            </div>
          ) : null}
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
        {paths.map((p) => <div key={p} className="text-secondary font-mono">{p}</div>)}
        <Pre>
          {text}
          {truncated > 0 && <span className="text-tertiary">{`\n... ${truncated} more lines`}</span>}
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
        {input.pattern ? <div className="font-mono">pattern: {show(input.pattern)}</div> : null}
        {input.path ? <div className="font-mono">path: {show(input.path)}</div> : null}
        {input.type ? <div className="text-tertiary">type: {show(input.type)}</div> : null}
        {input.output_mode ? <div className="text-tertiary">mode: {show(input.output_mode)}</div> : null}
      </div>
    )
  }

  if (payload === PAYLOAD.NAME && !carriesMore(toolInput, PAYLOAD_KEYS[PAYLOAD.NAME])) {
    return <div className="font-mono">{nameOf(name, toolInput)}</div>
  }

  if (payload === PAYLOAD.QUERY) {
    return (
      <div className="space-y-0.5">
        {input.query ? <div className="font-mono">query: {show(input.query)}</div> : null}
        {input.max_results ? <div className="text-tertiary">Max results: {show(input.max_results)}</div> : null}
      </div>
    )
  }

  if (payload === PAYLOAD.TODOS) {
    const todos = payloadValue(name, toolInput)
    const list = Array.isArray(todos) ? todos : []
    return (
      <div className="space-y-0.5">
        {list.map((t: unknown, i: number) => {
          const item = isRecord(t) ? t : {}
          return (
          <div key={i} className="flex items-center gap-2">
            {item.status === 'completed' ? (
              <span className="text-status-success-fg text-3xs">&#10003;</span>
            ) : (
              <span className="inline-block w-2.5 h-2.5 rounded-full border border-status-info-solid" />
            )}
            <span className={item.status === 'completed' ? 'line-through text-tertiary' : ''}>
              {show(item.content)}
            </span>
          </div>
          )
        })}
      </div>
    )
  }

  // Default: formatted JSON for MCP tools and others
  const json = JSON.stringify(toolInput, null, 2)
  const { text, truncated } = truncateLines(json, 200)
  return (
    <Pre>
      {text}
      {truncated > 0 && <span className="text-tertiary">{`\n... ${truncated} more lines`}</span>}
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
  const wrapDetail = expanded && toolRole(name) === ROLE.SHELL && isRecord(toolInput) && !!toolInput.description

  // Show result summary inline (e.g., "15 lines", "3 results", "ok")
  const inlineSummary = resultSummary || ''

  return (
    <div className="my-1.5 rounded-card bg-surface-sunken text-xs overflow-hidden">
      <div
        className={`flex items-start gap-2 py-1 px-2 text-secondary ${expandable ? 'cursor-pointer hover:bg-surface-hover' : ''}`}
        onClick={expandable ? () => setExpanded(!expanded) : undefined}
      >
        {expandable && (
          <span className={`mt-0.5 shrink-0 text-3xs text-tertiary transform transition-transform ${expanded ? 'rotate-90' : ''}`}>
            &#9654;
          </span>
        )}
        <span className="mt-0.5 shrink-0">
          {status === 'running' ? (
            <span className="inline-block w-3 h-3 border-2 border-status-info-solid border-t-transparent rounded-full animate-spin" />
          ) : status === 'failed' ? (
            <span className="text-status-danger-fg" title="Stopped before completion">&#10007;</span>
          ) : (
            <span className="text-status-success-fg">&#10003;</span>
          )}
        </span>
        <span className="font-medium text-primary shrink-0">{name}</span>
        {detail && (
          <span
            className={`text-tertiary font-mono text-2xs ${wrapDetail ? 'whitespace-normal break-words min-w-0' : 'truncate'}`}
            title={detail}
          >
            {detail}
          </span>
        )}
        {inlineSummary && status === 'done' && (
          <span className="text-tertiary text-2xs ml-auto shrink-0">
            {inlineSummary}
          </span>
        )}
      </div>
      {expanded && (toolInput || toolResult) && (
        <div className="border-t border-subtle px-3 py-2 text-xs font-mono text-primary space-y-2">
          {toolInput ? <ToolDetail name={name} toolInput={toolInput} /> : null}
          {toolResult && (
            <div>
              <div className="text-secondary font-sans font-medium mb-1">Output</div>
              <pre className="whitespace-pre-wrap bg-surface-sunken rounded-sm px-2 py-1.5 max-h-80 overflow-y-auto text-primary">
                {toolResult}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
