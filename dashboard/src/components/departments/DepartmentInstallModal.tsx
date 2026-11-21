import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'

import {
  type CommunityAgentRegistryEntry,
  type CommunityDepartmentSet,
  type SetInstallJob,
  USE_NEW,
  useInstallDepartmentSet,
  useSetInstallStatus,
  useSetPreview,
} from '../../api/communityAgents'
import { ApiError, errorText } from '../../lib/http/apiJson'
import { SET_MEMBER, setMemberWord } from '../../lib/kinds/card'
import { SET_INSTALL_STATUS } from '../../lib/status/setInstall'
import { departmentSetupLines, jobLines, newIdsLine, optionLabel } from '../../lib/status/departmentSet'
import { unionTools } from '../../lib/status/requiredTools'
import RequiredTools from '../RequiredTools'
import { DetailsToggle } from '../apps/AppApprovalCard'
import TemplateApps from '../common/TemplateApps'
import TemplatePanel from '../common/TemplatePanel'
import Button from '../ui/Button'
import Dialog from '../ui/Dialog'
import { Input, Select } from '../ui/Field'
import { DIALOG_PANEL } from '../ui/classes'

/**
 * Installing a department set from the community catalog (the proxy's
 * department_set_installer): the template panel the store shows in Show
 * details, for a department (what it is, its text, what it brings, its team
 * with a note for an agent renamed or already installed), with the
 * department's name in its slot and its two folded lines for the whole
 * department, the tools and the apps with the one approval; then the job's
 * progress row by row until the department stands.
 */

interface Props {
  open: boolean
  set: CommunityDepartmentSet | null
  onClose: () => void
  /** The catalog's templates by slug: each agent's line of what it does. */
  templates?: Record<string, CommunityAgentRegistryEntry>
}

export default function DepartmentInstallModal({ open, set, onClose, templates = {} }: Props) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const install = useInstallDepartmentSet()
  const [name, setName] = useState(set?.name ?? '')
  const [approve, setApprove] = useState(true)
  // Each member's choice the person made here; the others show the plan's own.
  const [use, setUse] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const [jobId, setJobId] = useState<string | null>(null)

  // The dialog opening, or its set changing while open, starts the form over.
  const [openedFor, setOpenedFor] = useState({ open, set })
  if (openedFor.open !== open || openedFor.set !== set) {
    setOpenedFor({ open, set })
    if (open) {
      setName(set?.name ?? '')
      setApprove(true)
      setUse({})
      setError('')
      setJobId(null)
    }
  }

  const preview = useSetPreview(open && set ? set.slug : null, name, use)
  // A choice the plan refuses (its agent changed meanwhile) is dropped, so the
  // selects show the plan's own again. A plan with no choices refuses none.
  const refused = preview.error instanceof ApiError && preview.error.status === 400 && Object.keys(use).length > 0
  if (refused) setUse({})
  const status = useSetInstallStatus(jobId, Boolean(jobId))
  const job: SetInstallJob | undefined = status.data
  const members = preview.data?.members ?? []
  const blocked = members.filter((m) => m.blocked_mcps.length > 0 || !m.platform_compat_ok)
  const consentItems = members.reduce((n, m) => n + m.apps.length + m.checks.length, 0)
  const running = Boolean(preview.data?.running_job) || install.isPending || (job?.status === SET_INSTALL_STATUS.RUNNING)
  // The team as the catalog lists it, then as the preview plans it.
  const team = members.length > 0 ? members : (set?.members ?? [])
  // One line for the tools of every agent the install makes (and a used
  // agent's blocked tool, which still holds the install), one for the apps.
  const tools = unionTools(members.filter((m) => m.action === 'new' || m.blocked_mcps.length > 0).map((m) => m.required_mcps))
  const groups = members.filter((m) => m.action === 'new')
    .map((m) => ({ key: m.template, title: m.display_name, apps: m.apps, checks: m.checks }))

  if (!open || !set) return null

  const submit = () => {
    setError('')
    // What the dialog shows is what the install does: every select's choice.
    const shown = Object.fromEntries(members.filter((m) => m.options.length > 0)
      .map((m) => [m.template, use[m.template] ?? m.choice]))
    install.mutate({ slug: set.slug, department_name: name.trim(), consent: approve && consentItems > 0, use: shown }, {
      onSuccess: (r) => setJobId(r.job_id),
      onError: (err) => setError(errorText(err) || 'The install did not start'),
    })
  }

  const openDepartment = () => {
    void qc.invalidateQueries({ queryKey: ['departments'] })
    void qc.invalidateQueries({ queryKey: ['agents'] })
    onClose()
    navigate('/agents?view=departments')
  }

  if (jobId) {
    const done = job && job.status !== SET_INSTALL_STATUS.RUNNING
    // The rows say how each agent stands. A line that says something was
    // not done shows under them, every other line folds under Details.
    const { notDone, rest } = jobLines(job)
    return (
      <Dialog onClose={done ? onClose : () => {}} aria-labelledby="set-install-title" busy={!done}
              panelClassName={`${DIALOG_PANEL} p-6 max-w-lg max-h-[90vh] overflow-y-auto`}>
        <h3 id="set-install-title" className="text-sm font-semibold text-primary mb-3">
          {done ? (job.status === SET_INSTALL_STATUS.DONE ? `${job.department_name} is installed` : `${job.department_name} was not installed`) : `Installing ${name.trim()}`}
        </h3>
        <ul className="space-y-1 text-xs mb-3" data-testid="set-install-members">
          {(job?.members ?? []).map((m) => (
            <li key={m.template} className="flex items-baseline justify-between gap-2">
              <span className="text-primary">{m.display_name} <span className="text-tertiary">({m.level})</span></span>
              <span className={m.status === SET_MEMBER.FAILED ? 'text-status-danger-fg' : 'text-secondary'}>
                {setMemberWord(m)}{m.detail ? `: ${m.detail}` : ''}
              </span>
            </li>
          ))}
        </ul>
        {notDone.length > 0 && (
          <ul className="space-y-1 text-xs text-status-warning-fg mb-3" data-testid="set-install-not-done">
            {notDone.map((l, i) => <li key={i}>{l}</li>)}
          </ul>
        )}
        <FoldedLines lines={rest} testId="set-install-details" listTestId="set-install-lines" className="mb-4" />
        {!done && <p className="text-xs text-tertiary mb-4">This takes a minute or two. You can close the page: the install goes on and you are told when it ends.</p>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" size="md" onClick={onClose}>Close</Button>
          {done && job.status === SET_INSTALL_STATUS.DONE && (
            <Button variant="primary" size="md" onClick={openDepartment}>Open the department</Button>
          )}
        </div>
      </Dialog>
    )
  }

  // Under the team, only what a person decides or should know: the choice
  // for a member the person has an agent for, the new agents' IDs when they
  // are not the templates' own, a template this OtoDock is too old for.
  const choosing = members.filter((m) => m.options.length > 0)
  const ids = newIdsLine(members)
  const old = members.filter((m) => !m.platform_compat_ok)
  return (
    <Dialog onClose={onClose} busy={install.isPending} aria-labelledby="set-install-title"
            panelClassName={`${DIALOG_PANEL} p-6 max-w-lg max-h-[90vh] overflow-y-auto`}>
      <h3 id="set-install-title" className="text-sm font-semibold text-primary mb-3">Install the {set.name} department</h3>
      {preview.isError && !refused && <p className="text-xs text-status-danger-fg mb-3">The catalog could not be read. Try again in a minute.</p>}
      <div className="mb-4" data-testid="set-members">
        <TemplatePanel
          kind="department"
          variant="full"
          name={set.name}
          color={set.color || set.members?.[0]?.color}
          version={set.version}
          author={set.author}
          agents={team.length}
          summary={set.summary}
          description={set.description}
          features={set.highlights ?? []}
          team={{
            members: team,
            levels: preview.data?.levels ?? set.levels ?? [],
            colors: templates,
            notes: (choosing.length > 0 || ids || old.length > 0) && (
              <div className="mt-2 space-y-2" data-testid="set-notes">
                {choosing.map((m) => (
                  <label key={m.template} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs"
                         data-testid={`set-member-${m.template}`}>
                    <span className="font-medium text-primary">{m.display_name}</span>
                    {/* A phone gives the select its own line, so its words are never cut. */}
                    <Select compact className="w-full min-w-0 sm:w-auto sm:flex-1" aria-label={`The agent for ${m.display_name}`}
                            value={use[m.template] ?? m.choice}
                            onChange={(e) => setUse((u) => ({ ...u, [m.template]: e.target.value }))}>
                      <option value={USE_NEW}>Install a new one</option>
                      {m.options.map((o) => <option key={o.slug} value={o.slug}>{optionLabel(o)}</option>)}
                    </Select>
                  </label>
                ))}
                {ids && <p className="text-2xs text-tertiary" data-testid="set-new-ids">{ids}</p>}
                {old.map((m) => (
                  <p key={m.template} className="text-2xs text-status-danger-fg" data-testid={`set-old-${m.template}`}>
                    {m.display_name} needs a newer OtoDock.
                  </p>
                ))}
              </div>
            ),
          }}
          slot={(
            <div>
              <label className="block text-xs font-medium text-secondary mb-1" htmlFor="set-dept-name">Department name</label>
              <Input id="set-dept-name" type="text" value={name} onChange={(e) => setName(e.target.value)} className="mb-1" autoFocus />
              <p className="text-xs text-tertiary">The library the head shares takes the same name.</p>
              {preview.data?.name_problem && <p className="text-xs text-status-danger-fg mt-1">{preview.data.name_problem}</p>}
              {(preview.data?.existing.length ?? 0) > 0 && (
                <p className="text-xs text-tertiary mt-1">From this set already: {preview.data!.existing.map((d) => d.name).join(', ')}. The same name uses that department again.</p>
              )}
            </div>
          )}
          folded={(
            <>
              {preview.isLoading && <p className="text-xs text-secondary">Checking what is installed…</p>}
              <RequiredTools tools={tools} />
              <TemplateApps
                apps={groups.flatMap((g) => g.apps)}
                checks={groups.flatMap((g) => g.checks)}
                groups={groups}
                approval={{
                  checked: approve,
                  onChange: setApprove,
                  scope: preview.data?.consent_scope === 'everyone' ? 'everyone' : 'own',
                  testId: 'set-consent-checkbox',
                  hint: 'Unticked, each app waits for approval on its card and each check is offered, not mandatory.',
                }}
              />
              <FoldedLines title="How it is set up" testId="setup-facts" lines={departmentSetupLines(preview.data ?? {
                library: set.library ?? null, schedules: set.schedules ?? [], mode: set.mode,
              })} />
            </>
          )}
        />
      </div>

      {error && <p className="text-xs text-status-danger-fg mb-3">{error}</p>}
      {preview.data?.running_job && <p className="text-xs text-status-warning-fg mb-3">This set is being installed already. Wait for it to finish.</p>}

      <div className="flex justify-end gap-2">
        <Button size="md" onClick={onClose}>Cancel</Button>
        <Button variant="primary" size="md" onClick={submit}
                disabled={running || !name.trim() || Boolean(preview.data?.name_problem) || blocked.length > 0 || members.length === 0
                          // The rows are an earlier plan's until the plan of what the selects show arrives.
                          || preview.isPlaceholderData}>
          {install.isPending ? 'Starting…' : 'Install the department'}
        </Button>
      </div>
    </Dialog>
  )
}

/** Lines folded under one Details, closed until asked: after a title (how
 *  the department is set up) or alone (the install's result). */
function FoldedLines({ title, lines, testId, listTestId, className = '' }: {
  title?: string
  lines: string[]
  testId: string
  listTestId?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  if (!lines.length) return null
  return (
    <div className={`text-xs ${className}`} data-testid={testId}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        {title && <span className="font-medium text-primary">{title}</span>}
        <DetailsToggle open={open} onToggle={() => setOpen((o) => !o)} />
      </div>
      {open && (
        <ul className="mt-1.5 space-y-1 text-secondary" data-testid={listTestId}>
          {lines.map((l, i) => <li key={i}>{l}</li>)}
        </ul>
      )}
    </div>
  )
}
