import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  type CheckItem, type CheckVerdict, useChecks, useDeleteCheck, useCheckVerdicts, useCheckSettings,
  useSaveCheckSettings, conditionWords,
} from '../../api/checks'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'
import { isLocalTarget } from '../../lib/placement'
import { toastError } from '../../lib/http/apiJson'
import Button from '../../components/ui/Button'
import { Input } from '../../components/ui/Field'
import InfoTip from '../../components/ui/InfoTip'
import { BADGE_BASE, BADGE_TONE, CARD } from '../../components/ui/classes'
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

  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>
  const items = data?.checks ?? []
  const agents = items.filter((c) => !c.owner)
  const mine = items.filter((c) => !!c.owner)

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold font-display text-primary">Checks</h1>
          <p className="text-sm text-secondary">
            What judges <span className="font-medium text-primary">{agent}</span>&apos;s work at the end of a turn
          </p>
        </div>
        <div className="flex gap-2">
          {data?.username && (
            <Button onClick={() => setEditing({ own: true })} data-testid="new-private-check"
                    className="self-start sm:self-auto shrink-0">
              + My check
            </Button>
          )}
          {canManage && (
            <Button variant="primary" onClick={() => setEditing({ own: false })} data-testid="new-check"
                    className="self-start sm:self-auto shrink-0">
              + New check
            </Button>
          )}
        </div>
      </div>

      {data?.tool_enabled === false && (
        <p className="rounded-card border border-status-warning-border bg-status-warning-bg px-3 py-2 text-sm text-status-warning-fg" data-testid="checks-tool-off">
          The checks tool is off for this agent. Its sessions cannot list, attach or run checks. The agent cannot
          create any either. Mandatory checks still run.
          {canManage && <> Turn on <span className="font-medium">Checks</span> under <Link className="underline" to={`/agents/${agent}/mcps`}>MCPs</Link>.</>}
        </p>
      )}

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-secondary uppercase">The agent&apos;s checks</h2>
        {agents.length === 0 ? (
          <div className={`${CARD} p-6 text-center`}>
            <p className="text-sm text-tertiary">
              {canManage ? 'No checks yet. A check judges the agent’s work and hands it its findings for another round.'
                : 'No checks offered on this agent yet.'}
            </p>
          </div>
        ) : agents.map((c) => (
          <CheckRow key={c.ref} item={c} canEdit={canManage} agent={agent}
                    onEdit={() => setEditing({ item: c, own: false })}
                    onDelete={() => { del.mutateAsync({ agent, name: c.name }).catch(toastError('The check was not removed')) }} />
        ))}
      </section>

      {data?.username && (
        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-secondary uppercase">My checks</h2>
          {mine.length === 0 ? (
            <p className="text-sm text-tertiary">None yet. A private check runs on your own sessions once you attach it. In a chat, use the checks tool: <code className="font-mono text-xs">attach_check</code>.</p>
          ) : mine.map((c) => (
            <CheckRow key={c.ref} item={c} canEdit agent={agent}
                      onEdit={() => setEditing({ item: c, own: true })}
                      onDelete={() => { del.mutateAsync({ agent, name: c.name, own: true }).catch(toastError('The check was not removed')) }} />
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
    <div className={`${CARD} p-4 space-y-1.5`} data-testid={`check-${item.ref}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-primary">{item.name}</span>
        {item.description && <InfoTip label={item.name}>{item.description}</InfoTip>}
        <span className={`${BADGE_BASE} ${item.mandatory ? BADGE_TONE.warning : BADGE_TONE.info}`}>
          {item.owner ? 'mine' : item.mandatory ? 'mandatory' : 'offered'}
        </span>
        {item.sections.map((s) => (
          <span key={s} className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>{s}</span>
        ))}
        {(item.problems?.length ?? 0) > 0 && (
          <span className="text-xs text-status-danger-fg" title={item.problems!.join(' · ')}>Needs a fix</span>
        )}
        {canEdit && (
          <span className="ml-auto flex gap-2 text-xs">
            <button className="text-link hover:text-link-hover" onClick={onEdit}>Edit</button>
            {confirm ? (
              <>
                <button className="text-status-danger-fg" onClick={onDelete} data-testid={`delete-${item.ref}`}>Remove for good</button>
                <button className="text-secondary hover:text-primary" onClick={() => setConfirm(false)}>Keep</button>
              </>
            ) : (
              <button className="text-secondary hover:text-primary" onClick={() => setConfirm(true)}>Remove</button>
            )}
          </span>
        )}
      </div>
      <p className="text-xs text-tertiary">
        Applies to {item.applies.join(', ')} · runs {conditionWords(item.condition)} · {item.rounds === 0 ? 'reports only' : `up to ${item.rounds} fix round${item.rounds === 1 ? '' : 's'}`}
        {item.updated_by && item.updated_at && <> · updated {item.updated_by === 'file' ? 'by file' : ''} {new Date(item.updated_at).toLocaleString()}</>}
        {item.owner === '' && <> · <Link className="text-link hover:text-link-hover" to={`/agents/${agent}/checks`}>agent:{item.name}</Link></>}
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
    <section className={`${CARD} p-4 space-y-2`} data-testid="check-spend">
      <div>
        <h2 className="inline text-sm font-semibold text-secondary uppercase">Judge spend</h2>
        <InfoTip label="Judge spend">
          Once the daily cap is reached, judges are skipped for the rest of the day. The verdict says so.
        </InfoTip>
      </div>
      <p className="text-xs text-secondary">
        A judge run costs what a task run of this agent costs.
        {data?.platform_default_usd != null ? ` The platform's default daily cap is $${data.platform_default_usd}.` : ' The platform sets no default daily cap.'}
      </p>
      <div className="flex items-center gap-2 text-sm">
        <span className="text-secondary">Daily cap in USD, blank for {data?.platform_default_usd != null ? 'the platform default' : 'none'}</span>
        <Input compact className="max-w-28 shrink-0"
               value={shown} onChange={(e) => setValue(e.target.value)} placeholder="none" data-testid="check-cap" />
        <Button variant="primary" size="xs"
                disabled={save.isPending || value === null || !valid}
                onClick={() => {
                  save.mutateAsync({ agent, daily_cap_usd: cap })
                    .then(() => setValue(null))
                    .catch(toastError('The daily cap was not saved'))
                }}>
          Save
        </Button>
      </div>
    </section>
  )
}

const STATUS_CLS: Record<CheckVerdict['status'], string> = {
  pass: BADGE_TONE.success,
  fail: BADGE_TONE.danger,
  error: BADGE_TONE.warning,
  skipped: BADGE_TONE.neutral,
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
      <h2 className="text-sm font-semibold text-secondary uppercase">Verdicts</h2>
      {verdicts.length === 0 ? (
        <p className="text-sm text-tertiary">{cursors.length ? 'No older verdicts.' : 'No verdicts yet.'}</p>
      ) : (
        <div className={`${CARD} overflow-x-auto`}>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-secondary border-b border-subtle bg-surface-sunken">
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
                <tr key={v.id} className="border-b border-subtle last:border-0 align-top">
                  <td className="px-3 py-2 whitespace-nowrap text-secondary">{new Date(v.created_at).toLocaleString()}</td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    <span className="font-medium text-primary">{v.check_name}</span>
                    <span className="text-xs text-tertiary"> · {v.section} · round {v.round}</span>
                    {v.chat_id && <><br /><Link className="text-xs text-link hover:text-link-hover" to={`/chat/${agent}/${v.chat_id}`}>Open the chat</Link></>}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    <span className={`${BADGE_BASE} ${STATUS_CLS[v.status]}`}>{v.status}</span>
                    {v.score != null && <span className="ml-1 text-xs text-tertiary">{v.score}</span>}
                  </td>
                  <td className="px-3 py-2 text-secondary max-w-md">
                    {v.summary || v.reason}
                    {v.findings.length > 0 && (
                      <ul className="mt-1 text-xs text-tertiary list-disc pl-4">
                        {v.findings.slice(0, 3).map((f, i) => <li key={i}>{f.location ? `${f.location}: ` : ''}{f.text}</li>)}
                        {v.findings.length > 3 && <li>… {v.findings.length - 3} more</li>}
                      </ul>
                    )}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap text-secondary">{isLocalTarget(v.ran_on) ? 'platform' : `machine ${v.ran_on.slice(0, 8)}`}</td>
                  <td className="px-3 py-2 text-right whitespace-nowrap text-secondary">{v.cost_usd ? `$${v.cost_usd.toFixed(2)}` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(cursors.length > 0 || hasOlder) && (
        <div className="flex items-center gap-2 text-xs" data-testid="verdict-pages">
          <Button
            size="xs"
            onClick={() => setCursors((c) => c.slice(0, -1))}
            disabled={cursors.length === 0 || isFetching}
            data-testid="verdicts-newer"
          >
            Newer
          </Button>
          <span className="text-tertiary">page {cursors.length + 1}</span>
          <Button
            size="xs"
            onClick={() => setCursors((c) => [...c, verdicts[verdicts.length - 1].created_at])}
            disabled={!hasOlder || isFetching}
            data-testid="verdicts-older"
          >
            Older
          </Button>
        </div>
      )}
    </section>
  )
}
