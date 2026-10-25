/**
 * Update an installed community agent to its template's catalog version
 * (COMMUNITY-AGENTS-REGISTRY.md "Updates"): the plan says what is replaced
 * (still as installed), added, and kept (edited locally, the new version
 * stored beside it); the new version's apps and checks take the same
 * consent as the install dialog; the apply runs as a job whose report
 * lands here and by notification.
 */

import { useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import {
  useApplyTemplateUpdate,
  useTakeNewVersion,
  useTemplateUpdatePlan,
  useTemplateUpdateStatus,
  taskPinNotes,
  TASK_PINS_HEADING,
  type TemplateUpdateKept,
  type TemplateUpdateReport,
} from '../api/communityAgents'
import { refusalProblems } from '../lib/http/apiJson'
import { DetailsToggle, ManifestBlocks, ManifestSummary } from './apps/AppApprovalCard'
import Dialog from './ui/Dialog'
import Button from './ui/Button'
import { DIALOG_PANEL } from './ui/classes'

interface Props {
  open: boolean
  agentSlug: string
  onClose: () => void
}

export default function AgentUpdateModal({ open, agentSlug, onClose }: Props) {
  const plan = useTemplateUpdatePlan(open ? agentSlug : null)
  const applyUpdate = useApplyTemplateUpdate()
  const takeNew = useTakeNewVersion()
  const qc = useQueryClient()
  const [approveAll, setApproveAll] = useState(true)
  const [openDetails, setOpenDetails] = useState<Record<string, boolean>>({})
  const [started, setStarted] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [taken, setTaken] = useState<Record<string, string>>({})
  const status = useTemplateUpdateStatus(agentSlug, started)

  const report: TemplateUpdateReport | null =
    started && status.data && !status.data.running && status.data.status === 'done'
      ? status.data.report ?? null
      : null
  // A job the platform lost (it restarted while the update ran) leaves no
  // status at all: that is a failure to retry, never "Updating…" forever.
  const lost = started && status.data && !status.data.running && !status.data.status
  const failed = started && status.data && !status.data.running && (status.data.status === 'failed' || lost)

  useEffect(() => {
    if (!open) {
      setStarted(false)
      setError(null)
      setTaken({})
    }
  }, [open])
  useEffect(() => {
    if (report || failed) {
      qc.invalidateQueries({ queryKey: ['community-agents'] })
      qc.invalidateQueries({ queryKey: ['agent-info', agentSlug] })
    }
  }, [report, failed, qc, agentSlug])

  const consentApps = useMemo(() => (plan.data?.apps ?? []).filter(a => a.change !== 'same'), [plan.data])
  const consentChecks = useMemo(() => (plan.data?.checks ?? []).filter(c => c.change !== 'same'), [plan.data])
  const hasConsentItems = consentApps.length > 0 || consentChecks.length > 0

  if (!open) return null

  const submit = async () => {
    if (!plan.data) return
    setError(null)
    setStarted(false)
    // The last job's answer must not stand in for this one's.
    qc.removeQueries({ queryKey: ['template-update-status', agentSlug] })
    try {
      await applyUpdate.mutateAsync({
        agent_slug: agentSlug,
        from_version: plan.data.from_version ?? '',
        ...(approveAll && hasConsentItems
          ? {
              approve_apps: Object.fromEntries(consentApps.map(a => [a.slug, a.sig])),
              approve_checks: Object.fromEntries(consentChecks.map(c => [c.name, c.sig])),
            }
          : {}),
      })
      setStarted(true)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const take = async (k: TemplateUpdateKept) => {
    try {
      const r = await takeNew.mutateAsync({ agent_slug: agentSlug, new_path: k.new_path })
      setTaken(t => ({ ...t, [k.new_path]: r.status === 'replaced' ? 'taken' : r.status || 'done' }))
    } catch (e) {
      setTaken(t => ({ ...t, [k.new_path]: `failed: ${(e as Error).message}` }))
    }
  }

  const list = (title: string, items: string[]) =>
    items.length > 0 ? (
      <div className="mb-3">
        <div className="font-medium text-primary mb-1">{title}</div>
        <ul className="list-disc pl-5 space-y-0.5 text-secondary">
          {items.map((it, i) => <li key={i}>{it}</li>)}
        </ul>
      </div>
    ) : null

  const keptList = (all: TemplateUpdateKept[], withTake: boolean) => {
    const items = all.filter(k => k.reason !== 'removed locally')
    const removed = all.filter(k => k.reason === 'removed locally')
    return (<>
    {removed.length > 0 && (
      <div className="mb-3" data-testid="removed-list">
        <div className="font-medium text-primary mb-1">Left out, because you removed them</div>
        <ul className="list-disc pl-5 space-y-0.5 text-secondary">
          {removed.map((k, i) => <li key={i}>{k.what}</li>)}
        </ul>
      </div>
    )}
    {items.length > 0 ? (
      <div className="mb-3">
        <div className="font-medium text-primary mb-1">
          Kept as you have them{withTake ? ', with the new version stored beside each' : ', with the new version to be stored beside each'}
        </div>
        <ul className="space-y-1 text-secondary">
          {items.map((k, i) => (
            <li key={i} className="flex flex-wrap items-center gap-x-2 gap-y-1" data-testid="kept-row">
              <span>{k.what}</span>
              <span className="text-tertiary">· {k.reason}</span>
              {k.path && <code className="text-2xs text-tertiary">{k.path}</code>}
              {withTake && k.new_path && (
                taken[k.new_path]
                  ? <span className="text-2xs text-tertiary">{taken[k.new_path]}</span>
                  : (
                    <Button type="button" onClick={() => take(k)} disabled={takeNew.isPending}>
                      Take the new version
                    </Button>
                  )
              )}
            </li>
          ))}
        </ul>
      </div>
    ) : null}
    </>)
  }

  const body = () => {
    if (report) {
      return (
        <div className="text-xs" data-testid="update-report">
          <p className="text-sm text-primary mb-3">
            Updated to <strong>{report.to_version}</strong>.
            {report.unchanged ? ` ${report.unchanged} piece(s) were already current.` : ''}
          </p>
          {list('Replaced', report.replaced)}
          {list('Added', report.added)}
          {keptList(report.kept, true)}
          {list('App releases waiting for approval on their cards', report.pending_apps)}
          {list('Checks offered, not mandatory', report.offered_checks)}
          {report.mcps?.requested?.length ? list('MCPs waiting for an admin', report.mcps.requested) : null}
          {report.notes?.length ? list('Notes', report.notes) : null}
          {list(TASK_PINS_HEADING, taskPinNotes(report))}
          {(report.members?.user_setup_kept ?? 0) > 0 && (
            <p className="text-tertiary">
              {report.members!.user_setup_kept} member(s) keep their own onboarding guide, edited since it was seeded.
            </p>
          )}
        </div>
      )
    }
    if (failed) {
      return (
        <p className="text-xs text-status-danger-fg">
          The update failed: {status.data?.error || (lost ? 'the platform restarted while it ran' : 'unknown error')}. What was applied stays. Press Update again to resume.
        </p>
      )
    }
    if (started) {
      return <p className="text-xs text-secondary">Updating… The report lands here and in your notifications.</p>
    }
    if (plan.isLoading) return <p className="text-xs text-secondary">Reading the community list and comparing…</p>
    if (plan.isError) {
      const problems = refusalProblems(plan.error)
      if (problems.length > 0) {
        return (
          <div className="text-xs text-status-danger-fg" data-testid="plan-problems">
            <p className="mb-1">The new version cannot be used:</p>
            <ul className="list-disc pl-5 space-y-0.5">
              {problems.map((it, i) => <li key={i}>{it}</li>)}
            </ul>
          </div>
        )
      }
      return <p className="text-xs text-status-danger-fg">{(plan.error as Error)?.message || 'The plan could not be read.'}</p>
    }
    const p = plan.data
    if (!p) return null
    return (
      <div className="text-xs">
        <p className="text-sm text-primary mb-3">
          <strong>{p.display_name}</strong> {p.from_version || 'unknown'} → <strong>{p.to_version}</strong>
          {!p.has_baseline && (
            <span className="block text-tertiary mt-1">
              This agent was installed before updates existed, so nothing is replaced. Every piece is kept, with the new version stored beside it. What the new version adds is added.
            </span>
          )}
        </p>
        {list('Will be replaced, unchanged since the install', p.replaced)}
        {list('Will be added', p.added)}
        {keptList(p.kept, false)}
        {p.unchanged > 0 && <p className="text-tertiary mb-3">{p.unchanged} piece(s) are already current.</p>}
        {p.mcps?.new?.length ? list('MCPs the new version needs', p.mcps.new) : null}
        {list(TASK_PINS_HEADING, taskPinNotes(p))}

        {hasConsentItems && (
          <div className="rounded-card border border-subtle p-3" data-testid="update-consent">
            {consentApps.length > 0 && (
              <div className="font-medium text-primary mb-2">
                {consentApps.length === 1 ? 'An app the new version brings or changes' : `${consentApps.length} apps the new version brings or changes`}
              </div>
            )}
            {consentApps.map(a => (
              <div key={a.slug} className="mb-3" data-testid={`consent-app-${a.slug}`}>
                <div className="flex flex-wrap items-baseline gap-x-1.5">
                  <span className="font-medium text-primary">{a.title}</span>
                  <span className="text-secondary">
                    {a.change === 'new' ? 'new · ' : 'changed · '}
                    {a.visibility === 'user' ? 'one copy for each member' : 'shared by the agent'}
                    {a.owner_approval ? ' · each member approves their own copy' : ''}
                  </span>
                </div>
                <ManifestSummary app={a.row} />
                <div className="mt-1">
                  <DetailsToggle
                    open={!!openDetails[a.slug]}
                    onToggle={() => setOpenDetails(d => ({ ...d, [a.slug]: !d[a.slug] }))}
                  />
                </div>
                {openDetails[a.slug] && <ManifestBlocks app={a.row} />}
              </div>
            ))}
            {consentChecks.length > 0 && (
              <div className="mb-2">
                <div className="font-medium text-primary mb-1">
                  {consentChecks.length === 1 ? 'A check the new version brings or changes' : `${consentChecks.length} checks the new version brings or changes`}
                </div>
                <ul className="space-y-1 text-secondary">
                  {consentChecks.map(c => (
                    <li key={c.name} data-testid={`consent-check-${c.name}`}>
                      <span className="font-mono text-2xs text-primary">{c.name}</span>
                      {c.mandatory ? <span>, mandatory</span> : null}
                      {c.description ? <span>: {c.description}</span> : null}
                      {c.words ? <span> · {c.words}</span> : null}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <label className="mt-2 flex items-start gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={approveAll}
                onChange={e => setApproveAll(e.target.checked)}
                className="mt-0.5 accent-action"
                data-testid="update-consent-checkbox"
              />
              <span className="text-primary">
                {p.consent_scope === 'everyone'
                  ? 'Approve these apps and checks for everyone who gets a copy'
                  : 'Approve these apps and checks: the shared ones and your own copy. Members approve theirs.'}
              </span>
            </label>
            <p className="mt-1 text-tertiary">
              Unticked, each new app release waits for approval on its card. Each check stays offered, not mandatory.
            </p>
          </div>
        )}
      </div>
    )
  }

  return (
    // The panel is bounded by the VISIBLE viewport (dvh: a phone's URL bar
    // takes part of 100vh) and its body is the one part that scrolls
    // (min-h-0, or a flex child grows to its content and pushes the footer
    // out of the clipped panel — the 2026-09-19 phone pass: the buttons cut
    // in half below the screen, unreachable by scrolling).
    <Dialog
      onClose={onClose}
      busy={applyUpdate.isPending}
      aria-labelledby="agent-update-title"
      panelClassName={`${DIALOG_PANEL} max-w-2xl max-h-[calc(100vh-1.5rem)] supports-[height:100dvh]:max-h-[calc(100dvh-1.5rem)] sm:max-h-[calc(100vh-4rem)] sm:supports-[height:100dvh]:max-h-[calc(100dvh-4rem)] flex flex-col overflow-hidden`}
    >
      <div className="contents" data-testid="update-modal-panel">
        <div className="shrink-0 flex items-center justify-between px-5 py-4 border-b border-subtle">
          <h3 id="agent-update-title" className="text-base font-semibold text-primary">Update from the template</h3>
          <button type="button" aria-label="Close" onClick={onClose} className="text-tertiary hover:text-primary text-lg leading-none">&times;</button>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4">
          {body()}
          {error && <p className="text-xs text-status-danger-fg mt-3">{error}</p>}
        </div>
        <div className="shrink-0 flex justify-end gap-2 px-5 py-3 border-t border-subtle">
          <Button size="md" onClick={onClose}>
            {report || failed ? 'Close' : 'Cancel'}
          </Button>
          {(!started || failed) && (
            <Button
              variant="primary"
              size="md"
              onClick={submit}
              disabled={!plan.data || applyUpdate.isPending}
              data-testid="update-submit"
            >
              {applyUpdate.isPending ? 'Starting…' : approveAll && hasConsentItems ? 'Update and approve' : 'Update'}
            </Button>
          )}
        </div>
      </div>
    </Dialog>
  )
}
