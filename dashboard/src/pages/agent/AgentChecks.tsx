import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  type CheckItem, type CheckVerdict, useChecks, useDeleteCheck, useCheckVerdicts, useCheckSettings,
  useSaveCheckSettings, conditionWords,
} from '../../api/checks'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'
import { isLocalTarget } from '../../lib/placement'
import { CheckEditorModal } from './AgentChecks.modals'

// The Checks page (proxy CHECKS.md "Governance"): the agent's checks
// (managers edit; everyone sees the offered ones), the caller's own checks
// on agents with personal sessions, the daily judge-spend cap, and the
// verdict list (a manager's is the agent's, a member's their own).

export default function AgentChecks() {
  const { name } = useParams<{ name: string }>()
  const agent = name || ''
  const { user } = useAuth()
  const canManage = canManageAgent(user, agent)
  const { data, isLoading } = useChecks(agent)
  const [editing, setEditing] = useState<{ item?: CheckItem; own: boolean } | null>(null)
  const del = useDeleteCheck()

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>
  const items = data?.checks ?? []
  const agents = items.filter((c) => !c.owner)
  const mine = items.filter((c) => !!c.owner)

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold text-p-text">Checks</h1>
          <p className="text-sm text-p-text-secondary">
            What judges <span className="font-medium text-p-text">{agent}</span>&apos;s work at the end of a turn
          </p>
        </div>
        <div className="flex gap-2">
          {data?.username && (
            <button onClick={() => setEditing({ own: true })} data-testid="new-private-check"
                    className="self-start sm:self-auto shrink-0 px-3 py-1.5 rounded-lg border border-p-border-light text-sm font-medium text-p-text">
              + My check
            </button>
          )}
          {canManage && (
            <button onClick={() => setEditing({ own: false })} data-testid="new-check"
                    className="self-start sm:self-auto shrink-0 px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover">
              + New check
            </button>
          )}
        </div>
      </div>

      {data?.tool_enabled === false && (
        <p className="rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-900/20 dark:border-amber-800 px-3 py-2 text-sm text-amber-900 dark:text-amber-200" data-testid="checks-tool-off">
          The checks tool is off for this agent: its sessions cannot list, attach or run checks, and the agent cannot
          create any. Mandatory checks still run.
          {canManage && <> Turn on <span className="font-medium">Checks</span> under <Link className="underline" to={`/agents/${agent}/mcps`}>MCPs</Link>.</>}
        </p>
      )}

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-p-text-secondary uppercase">The agent&apos;s checks</h2>
        {agents.length === 0 ? (
          <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-6 text-center">
            <p className="text-sm text-p-text-secondary">
              {canManage ? 'No checks yet. A check judges the agent’s work and hands it its findings for another round.'
                : 'No checks offered on this agent yet.'}
            </p>
          </div>
        ) : agents.map((c) => (
          <CheckRow key={c.ref} item={c} canEdit={canManage} agent={agent}
                    onEdit={() => setEditing({ item: c, own: false })}
                    onDelete={() => del.mutate({ agent, name: c.name })} />
        ))}
      </section>

      {data?.username && (
        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-p-text-secondary uppercase">My checks</h2>
          {mine.length === 0 ? (
            <p className="text-sm text-p-text-secondary">None yet — a private check runs on your own sessions when you attach it (the checks tool in a chat: <code className="font-mono text-xs">attach_check</code>).</p>
          ) : mine.map((c) => (
            <CheckRow key={c.ref} item={c} canEdit agent={agent}
                      onEdit={() => setEditing({ item: c, own: true })}
                      onDelete={() => del.mutate({ agent, name: c.name, own: true })} />
          ))}
        </section>
      )}

      {canManage && <SpendCap agent={agent} />}

      <VerdictList agent={agent} />

      {editing && (
        <CheckEditorModal agent={agent} existing={editing.item} own={editing.own} onClose={() => setEditing(null)} />
      )}
    </div>
  )
}

function CheckRow({ item, canEdit, agent, onEdit, onDelete }: {
  item: CheckItem; canEdit: boolean; agent: string; onEdit: () => void; onDelete: () => void
}) {
  const [confirm, setConfirm] = useState(false)
  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4 space-y-1.5" data-testid={`check-${item.ref}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-p-text">{item.name}</span>
        <span className={`px-2 py-0.5 rounded-full text-xs ${item.mandatory
          ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300'
          : 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300'}`}>
          {item.owner ? 'mine' : item.mandatory ? 'mandatory' : 'offered'}
        </span>
        {item.sections.map((s) => (
          <span key={s} className="px-2 py-0.5 rounded-full text-xs border border-p-border-light text-p-text-secondary">{s}</span>
        ))}
        {(item.problems?.length ?? 0) > 0 && (
          <span className="text-xs text-red-600" title={item.problems!.join('; ')}>needs a fix</span>
        )}
        {canEdit && (
          <span className="ml-auto flex gap-2 text-xs">
            <button className="text-brand" onClick={onEdit}>Edit</button>
            {confirm ? (
              <>
                <button className="text-red-600" onClick={onDelete} data-testid={`delete-${item.ref}`}>Remove for good</button>
                <button className="text-p-text-secondary" onClick={() => setConfirm(false)}>Keep</button>
              </>
            ) : (
              <button className="text-p-text-secondary" onClick={() => setConfirm(true)}>Remove</button>
            )}
          </span>
        )}
      </div>
      {item.description && <p className="text-sm text-p-text-secondary">{item.description}</p>}
      <p className="text-xs text-p-text-light">
        Applies to {item.applies.join(', ')} · runs {conditionWords(item.condition)} · {item.rounds === 0 ? 'reports only' : `up to ${item.rounds} fix round${item.rounds === 1 ? '' : 's'}`}
        {item.updated_by && item.updated_at && <> · updated {item.updated_by === 'file' ? 'by file' : ''} {new Date(item.updated_at).toLocaleString()}</>}
        {item.owner === '' && <> · <Link className="text-brand" to={`/agents/${agent}/checks`}>agent:{item.name}</Link></>}
      </p>
    </div>
  )
}

function SpendCap({ agent }: { agent: string }) {
  const { data } = useCheckSettings(agent)
  const save = useSaveCheckSettings()
  const [value, setValue] = useState<string | null>(null)
  const shown = value ?? (data?.daily_cap_usd == null ? '' : String(data.daily_cap_usd))
  // Text that is not an amount would reach the route as null (JSON has no
  // NaN) and clear the cap: it is not saved at all.
  const cap = shown.trim() === '' ? null : Number(shown)
  const valid = cap === null || (Number.isFinite(cap) && cap >= 0)
  return (
    <section className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4 space-y-2" data-testid="check-spend">
      <h2 className="text-sm font-semibold text-p-text-secondary uppercase">Judge spend</h2>
      <p className="text-xs text-p-text-secondary">
        A judge run costs what a task run of this agent costs. Once a daily cap is reached, the judges are skipped for the rest of the day and the verdict says so.
        {data?.platform_default_usd != null ? ` The platform default is $${data.platform_default_usd}.` : ' The platform sets no default.'}
      </p>
      <div className="flex items-center gap-2 text-sm">
        <span className="text-p-text-secondary">Daily cap (USD, blank = {data?.platform_default_usd != null ? 'the platform default' : 'none'})</span>
        <input className="w-28 px-2 py-1 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
               value={shown} onChange={(e) => setValue(e.target.value)} placeholder="none" data-testid="check-cap" />
        <button className="px-3 py-1 rounded-lg bg-brand text-white text-xs font-medium disabled:opacity-50"
                disabled={save.isPending || value === null || !valid}
                onClick={() => save.mutate({ agent, daily_cap_usd: cap },
                  { onSuccess: () => setValue(null) })}>
          Save
        </button>
      </div>
    </section>
  )
}

const STATUS_CLS: Record<CheckVerdict['status'], string> = {
  pass: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  fail: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
  error: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300',
  skipped: 'bg-p-bg text-p-text-secondary',
}

/** Verdicts per page; the list never grows past it (found on the VM pass
 * 2026-09-18: an agent judged every turn fills a page a day). */
export const VERDICT_PAGE = 20

export function VerdictList({ agent }: { agent: string }) {
  // A stack of page cursors: the last row's time of every page before this
  // one, so "Newer" walks back without a second fetch shape. One row past
  // the page says whether an older page exists.
  const [cursors, setCursors] = useState<string[]>([])
  const before = cursors[cursors.length - 1]
  const { data: rows = [], isFetching } = useCheckVerdicts(agent, { limit: VERDICT_PAGE + 1, before })
  const verdicts = rows.slice(0, VERDICT_PAGE)
  const hasOlder = rows.length > VERDICT_PAGE
  return (
    <section className="space-y-2" data-testid="check-verdicts">
      <h2 className="text-sm font-semibold text-p-text-secondary uppercase">Verdicts</h2>
      {verdicts.length === 0 ? (
        <p className="text-sm text-p-text-secondary">{cursors.length ? 'No older verdicts.' : 'No verdicts yet.'}</p>
      ) : (
        <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-p-text-secondary border-b border-p-border-light bg-p-bg/30">
                <th className="px-3 py-2 font-medium">When</th>
                <th className="px-3 py-2 font-medium">Check</th>
                <th className="px-3 py-2 font-medium">Verdict</th>
                <th className="px-3 py-2 font-medium">Summary</th>
                <th className="px-3 py-2 font-medium">Where</th>
                <th className="px-3 py-2 font-medium text-right">Cost</th>
              </tr>
            </thead>
            <tbody>
              {verdicts.map((v) => (
                <tr key={v.id} className="border-b border-p-border-light last:border-0 align-top">
                  <td className="px-3 py-2 whitespace-nowrap text-p-text-secondary">{new Date(v.created_at).toLocaleString()}</td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    <span className="font-medium text-p-text">{v.check_name}</span>
                    <span className="text-xs text-p-text-light"> · {v.section} · round {v.round}</span>
                    {v.chat_id && <><br /><Link className="text-xs text-brand" to={`/chat/${agent}/${v.chat_id}`}>open the chat</Link></>}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    <span className={`px-2 py-0.5 rounded-full text-xs ${STATUS_CLS[v.status]}`}>{v.status}</span>
                    {v.score != null && <span className="ml-1 text-xs text-p-text-light">{v.score}</span>}
                  </td>
                  <td className="px-3 py-2 text-p-text-secondary max-w-md">
                    {v.summary || v.reason}
                    {v.findings.length > 0 && (
                      <ul className="mt-1 text-xs text-p-text-light list-disc pl-4">
                        {v.findings.slice(0, 3).map((f, i) => <li key={i}>{f.location ? `${f.location} — ` : ''}{f.text}</li>)}
                        {v.findings.length > 3 && <li>… {v.findings.length - 3} more</li>}
                      </ul>
                    )}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap text-p-text-secondary">{isLocalTarget(v.ran_on) ? 'platform' : `machine ${v.ran_on.slice(0, 8)}`}</td>
                  <td className="px-3 py-2 text-right whitespace-nowrap text-p-text-secondary">{v.cost_usd ? `$${v.cost_usd.toFixed(2)}` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(cursors.length > 0 || hasOlder) && (
        <div className="flex items-center gap-2 text-xs" data-testid="verdict-pages">
          <button
            type="button"
            onClick={() => setCursors((c) => c.slice(0, -1))}
            disabled={cursors.length === 0 || isFetching}
            className="rounded-md border border-p-border-light px-2 py-1 text-p-text-secondary hover:bg-p-surface-hover disabled:opacity-50"
            data-testid="verdicts-newer"
          >
            Newer
          </button>
          <span className="text-p-text-light">page {cursors.length + 1}</span>
          <button
            type="button"
            onClick={() => setCursors((c) => [...c, verdicts[verdicts.length - 1].created_at])}
            disabled={!hasOlder || isFetching}
            className="rounded-md border border-p-border-light px-2 py-1 text-p-text-secondary hover:bg-p-surface-hover disabled:opacity-50"
            data-testid="verdicts-older"
          >
            Older
          </button>
        </div>
      )}
    </section>
  )
}
