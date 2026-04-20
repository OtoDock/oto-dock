import { useState } from 'react'

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

export function getToolDetail(name: string, summary: string | undefined, toolInput: any): string {
  // Bash: the model-written description says WHAT the command does — that's
  // the collapsed title (the command itself is the expanded detail). Wins
  // over `summary`, which older rows carry as the raw command. Generous cap:
  // collapsed rendering clips to one line via CSS, and the expanded pill
  // un-truncates this same text (see ToolActivity), so keep the full sentence.
  if (name === 'Bash' && toolInput?.description) {
    return truncate(toolInput.description, 300)
  }
  if (summary) return summary
  if (!toolInput) return ''
  switch (name) {
    case 'Bash':
      return toolInput.command ? truncate(toolInput.command, 120) : ''
    case 'Read':
    case 'Write':
    case 'Edit':
      return toolInput.file_path || ''
    case 'Grep':
      return [toolInput.pattern, toolInput.path].filter(Boolean).join(' in ')
    case 'Glob':
      return toolInput.pattern || ''
    case 'WebSearch':
      return toolInput.query || ''
    case 'WebFetch':
      return toolInput.url ? truncate(toolInput.url, 100) : ''
    case 'Agent':
      return toolInput.description || ''
    case 'Workflow':
      // Saved workflows carry `name`; inline scripts carry it in the meta
      // block — fish it out so the pill reads "Workflow review-changes".
      return toolInput.name
        || (typeof toolInput.script === 'string'
            ? (toolInput.script.match(/name:\s*['"]([^'"]+)['"]/)?.[1] ?? '')
            : '')
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

function ToolDetail({ name, toolInput }: { name: string; toolInput: any }) {
  if (!toolInput) return null

  if (name === 'Edit') {
    const fp = toolInput.file_path || ''
    const oldStr = toolInput.old_string || ''
    const newStr = toolInput.new_string || ''
    return (
      <div className="space-y-1.5">
        {fp && <div className="text-p-text-secondary font-mono">{fp}</div>}
        {oldStr && (
          <pre className="whitespace-pre-wrap bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-300 rounded-sm px-2 py-1.5 max-h-60 overflow-y-auto">
            {oldStr.split('\n').map((line: string, i: number) => (
              <span key={i}>{`- ${line}\n`}</span>
            ))}
          </pre>
        )}
        {newStr && (
          <pre className="whitespace-pre-wrap bg-green-50 dark:bg-green-900/20 text-green-800 dark:text-green-300 rounded-sm px-2 py-1.5 max-h-60 overflow-y-auto">
            {newStr.split('\n').map((line: string, i: number) => (
              <span key={i}>{`+ ${line}\n`}</span>
            ))}
          </pre>
        )}
      </div>
    )
  }

  if (name === 'Write') {
    const fp = toolInput.file_path || ''
    const content = toolInput.content || ''
    const { text, truncated } = truncateLines(content, 200)
    return (
      <div className="space-y-1.5">
        {fp && <div className="text-p-text-secondary font-mono">{fp}</div>}
        <pre className="whitespace-pre-wrap bg-p-surface dark:bg-p-bg rounded-sm px-2 py-1.5 max-h-80 overflow-y-auto">
          {text}
          {truncated > 0 && <span className="text-p-text-light">{`\n... (${truncated} more lines)`}</span>}
        </pre>
      </div>
    )
  }

  if (name === 'Bash') {
    // The description is the collapsed pill title (getToolDetail) — the
    // expanded body is the command itself.
    return (
      <pre className="whitespace-pre-wrap bg-p-surface dark:bg-p-bg rounded-sm px-2 py-1.5 max-h-60 overflow-y-auto">
        {toolInput.command || ''}
      </pre>
    )
  }

  if (name === 'Read') {
    return (
      <div className="space-y-0.5">
        <div className="font-mono">{toolInput.file_path || ''}</div>
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

  if (name === 'Grep') {
    return (
      <div className="space-y-0.5">
        {toolInput.pattern && <div className="font-mono">pattern: {toolInput.pattern}</div>}
        {toolInput.path && <div className="font-mono">path: {toolInput.path}</div>}
        {toolInput.type && <div className="text-p-text-light">type: {toolInput.type}</div>}
        {toolInput.output_mode && <div className="text-p-text-light">mode: {toolInput.output_mode}</div>}
      </div>
    )
  }

  if (name === 'TodoWrite') {
    const todos = Array.isArray(toolInput.todos) ? toolInput.todos : []
    return (
      <div className="space-y-0.5">
        {todos.map((t: any, i: number) => (
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
    <pre className="whitespace-pre-wrap bg-p-surface dark:bg-p-bg rounded-sm px-2 py-1.5 max-h-80 overflow-y-auto">
      {text}
      {truncated > 0 && <span className="text-p-text-light">{`\n... (${truncated} more lines)`}</span>}
    </pre>
  )
}

export default function ToolActivity({ name, summary, status, toolInput, toolResult, resultSummary }: Props) {
  const [expanded, setExpanded] = useState(false)
  const detail = getToolDetail(name, summary, toolInput)
  const expandable = !!toolInput || !!toolResult

  // Expanding un-truncates the collapsed title when it's a Bash description:
  // the full sentence wraps in place, reading above the command in the body.
  // The no-description fallback (title = the raw command, e.g. Codex) stays
  // clipped — the body already shows the command verbatim.
  const wrapDetail = expanded && name === 'Bash' && !!toolInput?.description

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
