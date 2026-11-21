import { useRef, useState } from 'react'
import {
  useChangeToolLimits, useToolLimits,
  type LimitedTool, type ToolLimit, type ToolLimitsChange, type ToolLimitsPage,
} from '../../api/outside'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'
import {
  ACCESS_LABEL, ALL_TOOLS, LIMIT_COLUMN, LIMIT_COLUMN_LABEL, OUTSIDE_ACCESS,
  type LimitAccess, type LimitColumn,
} from '../../lib/outside'
import Button from '../../components/ui/Button'
import Dialog from '../../components/ui/Dialog'
import InfoTip from '../../components/ui/InfoTip'
import { Select } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE } from '../../components/ui/classes'
import { CheckboxRow, SaveState, SectionCard } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'

export const TOOL_LIMITS_ID = 'tool-limits'

const NOTHING_READS = 'This tool has nothing that only reads, so nothing is used.'
const NOT_LISTED = 'Its tools are not listed yet. Save a choice and the list is read.'
const SOME_TOOLS = 'some'

/** The two columns a manager sets, in the card's order. */
const COLUMNS: LimitColumn[] = [LIMIT_COLUMN.RUNS, LIMIT_COLUMN.CHATS]

/** What the agent may use of each MCP, by who runs the session. Managers
 *  only; collapsed until opened. */
export function ToolLimitsSection({ name }: { name: string }) {
  const { user } = useAuth()
  if (!canManageAgent(user, name)) return null
  return <ToolLimitsCard name={name} />
}

/** The page with a change laid over it, until the server answers. */
function applied(page: ToolLimitsPage, change: ToolLimitsChange): ToolLimitsPage {
  const tools = page.tools.map((t) => {
    const next: LimitedTool = { ...t }
    for (const c of change.tools ?? []) {
      if (c.mcp !== t.mcp) continue
      for (const column of COLUMNS) {
        const limit = c[column]
        if (limit) next[column] = { access: limit.access, functions: limit.functions, changed: false }
      }
    }
    for (const r of change.remove ?? []) {
      if (r.mcp === t.mcp) next[r.governs] = null
    }
    return next
  })
  return { tools }
}

/** The word a cell shows for a limit, or for none. */
function wordOf(limit: ToolLimit | null): string {
  if (!limit) return ALL_TOOLS
  return limit.access === OUTSIDE_ACCESS.CUSTOM ? SOME_TOOLS : limit.access
}

function outsideWord(limit: ToolLimit | null): string {
  if (!limit) return 'Not added'
  return limit.access === OUTSIDE_ACCESS.CUSTOM ? 'Some tools' : ACCESS_LABEL[limit.access]
}

function ToolLimitsCard({ name }: { name: string }) {
  const { data } = useToolLimits(name)
  const change = useChangeToolLimits(name)
  const { stateOf, run } = useSaveState()
  const [pending, setPending] = useState<ToolLimitsChange[]>([])
  const chain = useRef<Promise<unknown>>(Promise.resolve())
  const [picking, setPicking] = useState<{ tool: LimitedTool; column: LimitColumn } | null>(null)

  if (!data) return null
  const view = pending.reduce(applied, data)

  const submit = (body: ToolLimitsChange) => {
    setPending((p) => [...p, body])
    chain.current = chain.current.then(() => run('limits', () => change.mutateAsync(body), {
      failTitle: 'This change was not saved',
    })).then((saved) => {
      setPending((p) => (saved ? p.filter((c) => c !== body) : []))
    })
  }

  const choose = (tool: LimitedTool, column: LimitColumn, word: string) => {
    if (word === ALL_TOOLS) {
      if (tool[column]) submit({ remove: [{ mcp: tool.mcp, governs: column }] })
    } else if (word === OUTSIDE_ACCESS.READ_ONLY) {
      submit({ tools: [{ mcp: tool.mcp, [column]: { access: OUTSIDE_ACCESS.READ_ONLY, functions: [] } }] })
    } else {
      setPicking({ tool, column })
    }
  }

  return (
    <div id={TOOL_LIMITS_ID} className="scroll-mt-4" data-testid="tool-limits">
      <SectionCard title="What the agent may use" defaultOpen={false}>
        <div>
          <p className="text-xs text-tertiary">
            Which tools of each MCP the agent may use. All tools unless you limit it.
            <InfoTip label="What the agent may use">
              A limit holds on every AI engine and in every mode. A tool left off is refused when the agent calls
              it. On OtoDock&apos;s own tools, a command the agent runs is refused too. A tool the MCP adds later stays
              off until you save the limit again. People&apos;s chats also covers a person&apos;s tasks, meetings and
              app buttons. A change applies when a chat or task next starts. A limit does not reach a service the
              agent can use another way. A tool may hold its own account token, as a Google tool does. Git and gh
              use the GitHub account.
            </InfoTip>
            <span className="normal-case tracking-normal"><SaveState state={stateOf('limits')} /></span>
          </p>
        </div>
        {view.tools.length === 0 && <p className="text-sm text-tertiary">This agent has no tools to limit.</p>}
        {view.tools.length > 0 && (
          <div className="divide-y divide-subtle [&>*]:py-3 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
            <div className="hidden md:grid md:grid-cols-[1fr_8rem_11rem_11rem] md:gap-3 text-xs font-semibold text-secondary">
              <span>Tool</span>
              <span>Outside people</span>
              {COLUMNS.map((c) => <span key={c}>{LIMIT_COLUMN_LABEL[c]}</span>)}
            </div>
            {view.tools.map((t) => <ToolRow key={t.mcp} tool={t} onChoose={choose} />)}
          </div>
        )}
      </SectionCard>
      {picking && (
        <SomeToolsDialog
          tool={picking.tool}
          column={picking.column}
          onClose={() => setPicking(null)}
          onSave={(functions) => {
            submit({ tools: [{ mcp: picking.tool.mcp, [picking.column]: { access: OUTSIDE_ACCESS.CUSTOM, functions } }] })
            setPicking(null)
          }}
        />
      )}
    </div>
  )
}

function ToolRow({ tool, onChoose }: {
  tool: LimitedTool
  onChoose: (tool: LimitedTool, column: LimitColumn, word: string) => void
}) {
  const nothingReads = tool.known_functions !== null && !tool.known_functions.some((f) => f.reads_only)
  const note = (limit: ToolLimit | null): string | null => {
    if (!limit || limit.access !== OUTSIDE_ACCESS.READ_ONLY) return null
    if (tool.known_functions === null) return NOT_LISTED
    return nothingReads ? NOTHING_READS : null
  }
  return (
    <div className="grid grid-cols-1 gap-2 md:grid-cols-[1fr_8rem_11rem_11rem] md:items-start md:gap-3" data-testid="limited-tool">
      <p className="text-sm text-primary min-w-0">
        {tool.label}
        {COLUMNS.some((c) => tool[c]?.changed) && (
          <span className={`ml-2 ${BADGE_BASE} ${BADGE_TONE.warning}`} title="The tool's maker changed which of its tools only read.">
            Changed
          </span>
        )}
      </p>
      <p className="text-xs text-tertiary md:pt-1.5">
        <span className="md:hidden">Outside people: </span>{outsideWord(tool.outside)}
      </p>
      {COLUMNS.map((column) => {
        const limit = tool[column]
        const word = wordOf(limit)
        const id = `limit-${tool.mcp}-${column}`
        return (
          <div key={column} className="flex flex-col gap-1">
            <label htmlFor={id} className="text-xs text-tertiary md:hidden">{LIMIT_COLUMN_LABEL[column]}</label>
            <Select id={id} compact aria-label={`${LIMIT_COLUMN_LABEL[column]}: ${tool.label}`} value={word}
              onChange={(e) => onChoose(tool, column, e.target.value)}>
              <option value={ALL_TOOLS}>All tools</option>
              <option value={OUTSIDE_ACCESS.READ_ONLY}>Only the tools that read</option>
              <option value={SOME_TOOLS}>Some tools…</option>
            </Select>
            {limit?.access === OUTSIDE_ACCESS.CUSTOM && (
              <p className="text-xs text-tertiary">{limit.functions.join(', ')}</p>
            )}
            {note(limit) && <p className="text-xs text-status-warning-fg">{note(limit)}</p>}
          </div>
        )
      })}
    </div>
  )
}

function SomeToolsDialog({ tool, column, onClose, onSave }: {
  tool: LimitedTool
  column: LimitColumn
  onClose: () => void
  onSave: (functions: string[]) => void
}) {
  const current = tool[column]
  const [ticked, setTicked] = useState<string[]>(current?.access === OUTSIDE_ACCESS.CUSTOM ? current.functions : [])
  const known = tool.known_functions ?? []
  const title = `${LIMIT_COLUMN_LABEL[column]}: ${tool.label}`
  return (
    <Dialog title={title} onClose={onClose}>
      <div className="p-4 flex flex-col gap-3">
        {tool.known_functions === null ? (
          <p className="text-sm text-tertiary">{NOT_LISTED}</p>
        ) : (
          <>
            <p className="text-xs text-tertiary">Tick the tools the agent may use.</p>
            <div className="flex flex-col gap-2 max-h-72 overflow-y-auto">
              {known.map((f) => (
                <CheckboxRow
                  key={f.name}
                  id={`limit-fn-${tool.mcp}-${column}-${f.name}`}
                  label={f.name}
                  description={f.reads_only ? 'Only reads.' : undefined}
                  checked={ticked.includes(f.name)}
                  onChange={(on) => setTicked(on ? [...ticked, f.name] : ticked.filter((n) => n !== f.name))}
                />
              ))}
            </div>
          </>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={ticked.length === 0} onClick={() => onSave(ticked)}>Save</Button>
        </div>
      </div>
    </Dialog>
  )
}

export type { LimitAccess }
