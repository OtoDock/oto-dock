import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { useAgentMcps } from '../../api/mcps'
import { useNotificationDefinitions, usePauseNotification, useResumeNotification } from '../../api/notifications'
import { useMyRemoteMachines } from '../../api/remoteMachines'
import { usePauseTask, useResumeTask, useRunTaskNow, useTasks } from '../../api/tasks'
import { usePauseTrigger, useResumeTrigger, useTriggers } from '../../api/triggers'
import { useAuth } from '../../contexts/AuthContext'
import { toastError } from '../../lib/http/apiJson'
import { CARD_WORDS, LIST_KIND, LIST_SCOPE, LIST_STATUS } from '../../lib/kinds/card'
import { canManageAgent } from '../../lib/permissions'
import { SCOPE } from '../../lib/visibility'
import { isReachableMachine, machineStateWord } from '../../lib/status/machine'
import { agentSettingsBase, userSettingsPath } from '../settings/sections'
import Button from '../ui/Button'
import ChatCard from '../ui/ChatCard'
import { BADGE_BASE, BADGE_TONE } from '../ui/classes'
import { useDialogs } from '../ui/dialogs'

/**
 * A list card in the chat (the proxy's services/cards/lists): the agent asked
 * for one of the pages' own lists, and the card is live. The row carries the
 * kind, the filters, the title and who asked for it, never a row
 * of the list: the rows are fetched here with the viewer's own sign-in,
 * through the page's own hooks, so they are never a copy, and the row
 * actions are the page's own calls gated by the row's `can_*` flags. Every
 * viewer of the chat sees their own rows, whoever's turn asked for the
 * card. A footer link opens the page.
 */

interface Props {
  kind: string
  filters: Record<string, string>
  title: string
  agentName?: string
}

export default function ListCard({ kind, filters, title, agentName = '' }: Props) {
  const { user } = useAuth()
  if (!user) return <ChatCard title={title} line={CARD_WORDS.listReading} testId="list-card" />
  const rows = { title, agent: agentName, filters }
  switch (kind) {
    case LIST_KIND.TASKS:
      return <TaskRows {...rows} />
    case LIST_KIND.TRIGGERS:
      return <TriggerRows {...rows} />
    case LIST_KIND.NOTIFICATIONS:
      return <NotificationRows {...rows} />
    case LIST_KIND.MACHINES:
      return <MachineRows {...rows} />
    case LIST_KIND.MCPS:
      if (!canManageAgent(user, agentName)) return <ChatCard title={title} line={CARD_WORDS.managersOnly} testId="list-card" />
      return <McpRows {...rows} />
    default:
      return <ChatCard title={title} line={CARD_WORDS.notInVersion} testId="list-card" />
  }
}

interface RowsProps { title: string; agent: string; filters: Record<string, string> }

interface Scoped { scope: string; enabled: boolean | number }

/** The page's list narrowed by the card's filters, as the pages narrow it:
 *  `scope` mine (the person's own rows, user scope), agent (the agent's own
 *  rows) or all; `status` active or paused. */
function narrow<T extends Scoped>(rows: T[], filters: Record<string, string>): T[] {
  return rows.filter((r) => {
    if (filters.scope === LIST_SCOPE.MINE && r.scope !== SCOPE.USER) return false
    if (filters.scope === LIST_SCOPE.AGENT && r.scope !== SCOPE.AGENT) return false
    if (filters.status === LIST_STATUS.ACTIVE && !r.enabled) return false
    if (filters.status === LIST_STATUS.PAUSED && r.enabled) return false
    return true
  })
}

const stateWord = (enabled: boolean | number) => (enabled ? 'Active' : 'Paused')

/** The rows a card shows; the rest is on the page its link opens. */
const MAX_ROWS = 12

/** The frame around a kind's rows: the count under the title, one line
 *  while the list loads, did not load or is empty, the page link below. */
function ListFrame({ title, loading, failed, count, noun, to, after, children }: {
  title: string; loading: boolean; failed: boolean; count: number; noun: [string, string]; to: string
  after?: ReactNode; children: ReactNode
}) {
  const more = count - MAX_ROWS
  let line: string | undefined
  if (loading) line = CARD_WORDS.listReading
  else if (failed) line = CARD_WORDS.listNotRead
  else if (count === 0) line = CARD_WORDS.listEmpty
  return (
    <ChatCard
      title={title}
      sub={line ? undefined : `${count} ${count === 1 ? noun[0] : noun[1]}`}
      line={line}
      testId="list-card"
      footer={<Link to={to} className="text-xs text-link hover:underline">{CARD_WORDS.openPage}</Link>}
    >
      {!line && <ul className="mt-2 divide-y divide-subtle" data-testid="list-card-rows">{children}</ul>}
      {!line && more > 0 && (
        <p className="mt-1 text-xs text-tertiary" data-testid="list-card-more">{`And ${more} more on the page.`}</p>
      )}
      {after}
    </ChatCard>
  )
}

function Row({ name, active, badge, children }: { name: string; active: boolean; badge: string; children?: ReactNode }) {
  return (
    <li className="flex items-center gap-2 py-1.5 text-sm" data-testid="list-card-row">
      <span className="min-w-0 flex-1 truncate text-primary">{name}</span>
      <span className={`${BADGE_BASE} ${active ? BADGE_TONE.success : BADGE_TONE.neutral}`}>{badge}</span>
      {children}
    </li>
  )
}

/** The page's own call a row action makes, with the row's id. */
interface RowMutation { mutateAsync: (id: string) => Promise<unknown>; isPending: boolean }

/** A row's action: the page's own call, its refusal toasted with the page's words. */
function RowAction({ label, mutation, id, failTitle }: { label: string; mutation: RowMutation; id: string; failTitle: string }) {
  return (
    <Button size="xs" disabled={mutation.isPending} onClick={() => { void mutation.mutateAsync(id).catch(toastError(failTitle)) }}>
      {label}
    </Button>
  )
}

function TaskRows({ title, agent, filters }: RowsProps) {
  const q = useTasks(agent)
  const run = useRunTaskNow()
  const pause = usePauseTask()
  const resume = useResumeTask()
  const dialogs = useDialogs()
  const [started, setStarted] = useState<{ name: string; runId: string } | null>(null)
  const rows = narrow(q.data ?? [], filters)
  // The page's own confirm and words, then a line with the run's link.
  const runNow = async (id: string, name: string) => {
    if (!(await dialogs.confirm({ title: `Run "${name}" now?`, confirmLabel: 'Run now' }))) return
    try {
      const result = await run.mutateAsync(id)
      setStarted({ name, runId: result.run_id })
    } catch (e) {
      toastError('The task was not run')(e)
    }
  }
  const after = started && (
    <p className="mt-2 text-xs text-secondary" data-testid="list-card-run" role="status">
      {`The run of "${started.name}" started. `}
      <Link to={`/runs/${started.runId}`} className="text-link hover:underline">{CARD_WORDS.openRun}</Link>
    </p>
  )
  return (
    <ListFrame title={title} loading={q.isLoading} failed={q.isError} count={rows.length} noun={['task', 'tasks']}
      to={`${agentSettingsBase(agent)}/scheduled-tasks`} after={after}>
      {rows.slice(0, MAX_ROWS).map((t) => (
        <Row key={t.id} name={t.name} active={t.enabled} badge={stateWord(t.enabled)}>
          {t.can_run && (
            <Button size="xs" disabled={run.isPending} onClick={() => { void runNow(t.id, t.name) }}>Run</Button>
          )}
          {t.can_pause && <RowAction label="Pause" mutation={pause} id={t.id} failTitle="The task was not paused" />}
          {t.can_resume && <RowAction label="Resume" mutation={resume} id={t.id} failTitle="The task was not resumed" />}
        </Row>
      ))}
    </ListFrame>
  )
}

function TriggerRows({ title, agent, filters }: RowsProps) {
  const q = useTriggers({ agent })
  const pause = usePauseTrigger()
  const resume = useResumeTrigger()
  const rows = narrow(q.data ?? [], filters)
  return (
    <ListFrame title={title} loading={q.isLoading} failed={q.isError} count={rows.length} noun={['trigger', 'triggers']}
      to={`${agentSettingsBase(agent)}/triggers`}>
      {rows.slice(0, MAX_ROWS).map((t) => (
        <Row key={t.id} name={t.name} active={t.enabled} badge={stateWord(t.enabled)}>
          {t.can_pause && <RowAction label="Pause" mutation={pause} id={t.id} failTitle="The trigger was not paused" />}
          {t.can_resume && <RowAction label="Resume" mutation={resume} id={t.id} failTitle="The trigger was not resumed" />}
        </Row>
      ))}
    </ListFrame>
  )
}

function NotificationRows({ title, agent, filters }: RowsProps) {
  const q = useNotificationDefinitions(agent)
  const pause = usePauseNotification()
  const resume = useResumeNotification()
  const rows = narrow(q.data ?? [], filters)
  return (
    <ListFrame title={title} loading={q.isLoading} failed={q.isError} count={rows.length} noun={['notification', 'notifications']}
      to={`${agentSettingsBase(agent)}/notifications`}>
      {rows.slice(0, MAX_ROWS).map((n) => (
        <Row key={n.id} name={n.title} active={Boolean(n.enabled)} badge={stateWord(n.enabled)}>
          {n.can_pause && <RowAction label="Pause" mutation={pause} id={n.id} failTitle="The notification was not paused" />}
          {n.can_resume && <RowAction label="Resume" mutation={resume} id={n.id} failTitle="The notification was not resumed" />}
        </Row>
      ))}
    </ListFrame>
  )
}

/** The person's machines, read-only: the name and the live state. */
function MachineRows({ title }: RowsProps) {
  const q = useMyRemoteMachines()
  const rows = q.data?.machines ?? []
  return (
    <ListFrame title={title} loading={q.isLoading} failed={q.isError} count={rows.length} noun={['machine', 'machines']}
      to={userSettingsPath('remote-machines')}>
      {rows.slice(0, MAX_ROWS).map((m) => (
        <Row key={m.id} name={m.name} active={isReachableMachine(m.status)} badge={machineStateWord(m.status)} />
      ))}
    </ListFrame>
  )
}

/** The agent's MCP servers, read-only (the page saves the whole set). */
function McpRows({ title, agent }: RowsProps) {
  const q = useAgentMcps(agent)
  const rows = q.data?.mcps ?? []
  return (
    <ListFrame title={title} loading={q.isLoading} failed={q.isError} count={rows.length} noun={['MCP server', 'MCP servers']}
      to={`${agentSettingsBase(agent)}/mcps`}>
      {rows.slice(0, MAX_ROWS).map((m) => (
        <Row key={m.name} name={m.label || m.name} active={m.enabled} badge={m.enabled ? 'Enabled' : 'Off'} />
      ))}
    </ListFrame>
  )
}
