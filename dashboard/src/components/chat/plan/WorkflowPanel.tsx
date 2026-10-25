import { useState, useEffect, useRef } from 'react'
import { pushEscHandler } from '../../../lib/escStack'
import { isRecord } from '../../../lib/json/guards'
import { GLASS, GLASS_HOVER } from '../../ui/glass'
import { POPOVER } from '../../ui/classes'

/** A live dynamic-workflow (Workflow tool) and its phase/agent tree.
 *  `progress` is the latest workflow_progress snapshot from the CLI (replace
 *  semantics). Entry shape is tolerant — the CLI may key fields slightly
 *  differently across versions, so we read several aliases. */
export interface WorkflowLive {
  toolUseId: string
  workflowName: string
  progress: unknown[]
  active: boolean
}

interface Props {
  workflows: WorkflowLive[]
}

function entryFields(e: unknown): Record<string, unknown> {
  return isRecord(e) ? e : {}
}
function entryPhase(e: unknown): unknown {
  const f = entryFields(e)
  return f.workflow_phase ?? f.phase ?? f.workflowPhase ?? null
}
function entryAgent(e: unknown): unknown {
  const f = entryFields(e)
  return f.workflow_agent ?? f.label ?? f.agent ?? f.workflowAgent ?? null
}
function entryState(e: unknown): unknown {
  const f = entryFields(e)
  return f.state ?? f.status ?? 'progress'
}

export default function WorkflowPanel({ workflows }: Props) {
  const [open, setOpen] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  const active = workflows?.filter((w) => w.active) || []

  // Count agent rows across active workflows (badge).
  let totalAgents = 0
  let doneAgents = 0
  for (const w of active) {
    for (const e of w.progress || []) {
      if (entryAgent(e) != null) {
        totalAgents += 1
        if (entryState(e) === 'done') doneAgents += 1
      }
    }
  }

  useEffect(() => {
    if (!open) return
    const handler = (ev: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(ev.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  useEffect(() => {
    if (!open) return
    return pushEscHandler(() => setOpen(false))
  }, [open])

  // Auto-hide when no active workflow (after all hooks).
  if (active.length === 0) return null

  const badge = totalAgents > 0 ? `${doneAgents}/${totalAgents}` : `${active.length}`

  return (
    <div ref={panelRef}>
      <button
        onClick={() => setOpen(!open)}
        title={`Workflow: ${active.length} running${totalAgents ? `, ${doneAgents}/${totalAgents} agents done` : ''}`}
        className={`relative flex size-control-md items-center justify-center rounded-xl ${GLASS} ${GLASS_HOVER}
                   transition-colors duration-fast`}
      >
        {/* Workflow / orchestration icon (branching nodes) */}
        <svg className="w-5 h-5 text-accent-agent-fg" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <circle cx="6" cy="6" r="2" strokeWidth={1.6} />
          <circle cx="18" cy="6" r="2" strokeWidth={1.6} />
          <circle cx="12" cy="18" r="2" strokeWidth={1.6} />
          <path d="M6 8v2a3 3 0 003 3h6a3 3 0 003-3V8M12 13v3" strokeWidth={1.4} strokeLinecap="round" />
        </svg>
        <span className="absolute -bottom-1 -right-1 min-w-[18px] h-[18px] rounded-full bg-accent-agent-fill text-on-solid text-3xs font-bold flex items-center justify-center px-1 border-2 border-surface-card">
          {badge}
        </span>
      </button>

      {open && (
        <div className={`mt-2 ${POPOVER} overflow-hidden
                        w-72 sm:w-80 max-w-[calc(100vw-2rem)]`}>
          <div className="flex items-center justify-between px-3 py-2 border-b border-subtle">
            <span className="text-xs font-medium text-secondary">Workflow</span>
            {totalAgents > 0 && (
              <span className="text-xs text-tertiary font-mono">{doneAgents}/{totalAgents} agents</span>
            )}
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {active.map((w) => (
              <div key={w.toolUseId} className="px-3 py-1.5">
                {w.workflowName && (
                  <div className="text-xs font-semibold text-primary mb-1 truncate">{w.workflowName}</div>
                )}
                {(w.progress || []).map((e: unknown, i: number) => {
                  const phase = entryPhase(e)
                  if (phase != null) {
                    return (
                      <div key={i} className="text-2xs font-medium text-accent-agent-fg mt-1.5 mb-0.5 uppercase tracking-wide">
                        {String(phase)}
                      </div>
                    )
                  }
                  const agent = entryAgent(e)
                  if (agent == null) return null
                  const st = entryState(e)
                  const done = st === 'done'
                  const tokens = entryFields(e).tokens ?? entryFields(e).token_count
                  return (
                    <div key={i} className="flex items-start gap-2 pl-2 py-0.5 text-xs">
                      <span className="mt-0.5 shrink-0">
                        {done ? (
                          <span className="text-status-success-fg text-2xs">&#10003;</span>
                        ) : (
                          <span className="inline-block w-2.5 h-2.5 rounded-full border-2 border-accent-agent-solid border-t-transparent animate-spin" />
                        )}
                      </span>
                      <span className={done ? 'text-tertiary' : 'text-primary'}>
                        {String(agent)}
                        {tokens ? <span className="text-tertiary"> · {String(tokens)} tok</span> : null}
                      </span>
                    </div>
                  )
                })}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
