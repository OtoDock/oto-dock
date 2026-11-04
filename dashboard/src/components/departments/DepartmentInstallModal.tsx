import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'

import {
  type CommunityDepartmentSet,
  type SetInstallJob,
  type SetPreviewMember,
  useInstallDepartmentSet,
  useSetInstallStatus,
  useSetPreview,
} from '../../api/communityAgents'
import { errorText } from '../../lib/http/apiJson'
import { SET_MEMBER, SET_MEMBER_WORDS, type SetMemberState } from '../../lib/kinds/card'
import { MODE_SUMMARY, REACH_SUMMARY, type DepartmentMode, type DepartmentReach } from '../../lib/kinds/department'
import { SET_INSTALL_STATUS } from '../../lib/status/setInstall'
import Button from '../ui/Button'
import Dialog from '../ui/Dialog'
import { Input } from '../ui/Field'
import { DIALOG_PANEL } from '../ui/classes'

/**
 * Installing a department set from the community catalog (the proxy's
 * department_set_installer): the department's name, what happens to each member (a new agent,
 * or an installed one the department takes), the library and the
 * schedules, one consent checkbox for every member's apps and checks, then
 * the job's progress row by row until the department stands.
 */

interface Props {
  open: boolean
  set: CommunityDepartmentSet | null
  onClose: () => void
}

const MEMBER_PLAN: Record<string, (m: SetPreviewMember) => string> = {
  new: (m) => (m.slug === m.template ? 'New' : `New, named ${m.slug}`),
  adopt: (m) => `Uses ${m.display_name} (${m.slug})`,
}

export default function DepartmentInstallModal({ open, set, onClose }: Props) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const install = useInstallDepartmentSet()
  const [name, setName] = useState(set?.name ?? '')
  const [approve, setApprove] = useState(true)
  const [fresh, setFresh] = useState<string[]>([])
  const [error, setError] = useState('')
  const [jobId, setJobId] = useState<string | null>(null)

  // The dialog opening, or its set changing while open, starts the form over.
  const [openedFor, setOpenedFor] = useState({ open, set })
  if (openedFor.open !== open || openedFor.set !== set) {
    setOpenedFor({ open, set })
    if (open) {
      setName(set?.name ?? '')
      setApprove(true)
      setFresh([])
      setError('')
      setJobId(null)
    }
  }

  const preview = useSetPreview(open && set ? set.slug : null, name, fresh)
  const status = useSetInstallStatus(jobId, Boolean(jobId))
  const job: SetInstallJob | undefined = status.data
  const members = preview.data?.members ?? []
  const blocked = members.filter((m) => m.blocked_mcps.length > 0 || !m.platform_compat_ok)
  const consentItems = members.reduce((n, m) => n + m.apps.length + m.checks.length, 0)
  const running = Boolean(preview.data?.running_job) || install.isPending || (job?.status === SET_INSTALL_STATUS.RUNNING)

  if (!open || !set) return null

  const submit = () => {
    setError('')
    install.mutate({ slug: set.slug, department_name: name.trim(), consent: approve && consentItems > 0, new: fresh }, {
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
                {SET_MEMBER_WORDS[m.status as SetMemberState] ?? m.status}{m.detail ? `: ${m.detail}` : ''}
              </span>
            </li>
          ))}
        </ul>
        {(job?.lines ?? []).length > 0 && (
          <ul className="list-disc pl-5 space-y-0.5 text-xs text-secondary mb-4" data-testid="set-install-lines">
            {job!.lines.map((l, i) => <li key={i}>{l}</li>)}
          </ul>
        )}
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

  return (
    <Dialog onClose={onClose} busy={install.isPending} aria-labelledby="set-install-title"
            panelClassName={`${DIALOG_PANEL} p-6 max-w-lg max-h-[90vh] overflow-y-auto`}>
      <h3 id="set-install-title" className="text-sm font-semibold text-primary mb-1">Install the {set.name} department</h3>
      <p className="text-xs text-secondary mb-4 line-clamp-3">{set.description}</p>

      <label className="block text-xs font-medium text-secondary mb-1" htmlFor="set-dept-name">Department name</label>
      <Input id="set-dept-name" type="text" value={name} onChange={(e) => setName(e.target.value)} className="mb-1" autoFocus />
      <p className="text-xs text-tertiary mb-3">
        The library the head shares takes the same name.
        {preview.data ? ` Delegation runs ${MODE_SUMMARY[preview.data.mode as DepartmentMode] ?? preview.data.mode}, ${REACH_SUMMARY[preview.data.reach as DepartmentReach] ?? preview.data.reach}.` : ''}
      </p>
      {preview.data?.name_problem && <p className="text-xs text-status-danger-fg mb-3">{preview.data.name_problem}</p>}

      <div className="mb-4 rounded-card border border-subtle p-3" data-testid="set-members">
        <div className="text-xs font-medium text-primary mb-2">The agents</div>
        {preview.isLoading && <div className="text-xs text-secondary">Checking what is installed…</div>}
        {preview.isError && <div className="text-xs text-status-danger-fg">The catalog could not be read. Try again in a minute.</div>}
        <ul className="space-y-1.5">
          {members.map((m) => (
            <li key={m.template} className="text-xs" data-testid={`set-member-${m.template}`}>
              <div className="flex flex-wrap items-baseline justify-between gap-x-2">
                <span className="text-primary">{m.display_name} <span className="text-tertiary">({m.level})</span></span>
                <span className="text-secondary">{(MEMBER_PLAN[m.action] ?? MEMBER_PLAN.new)(m)}</span>
              </div>
              {m.action === 'adopt' && (
                <button type="button" className="text-2xs underline text-tertiary"
                        onClick={() => setFresh((f) => [...f, m.template])}>
                  Install a new one instead
                </button>
              )}
              {m.placed.length > 0 && (
                <p className="text-2xs text-tertiary">Already in another department: {m.placed.join(', ')}.</p>
              )}
              {!m.platform_compat_ok && <p className="text-2xs text-status-danger-fg">Needs a newer OtoDock.</p>}
              {m.blocked_mcps.length > 0 && (
                <p className="text-2xs text-status-danger-fg">Not installed and not in the community list: {m.blocked_mcps.join(', ')}.</p>
              )}
            </li>
          ))}
        </ul>
        {preview.data?.library && (
          <p className="mt-2 text-xs text-secondary">
            The library: the head&apos;s {preview.data.library.subdir ? `${preview.data.library.subdir} folder` : 'knowledge folder'}, shared with the others {preview.data.library.writable ? 'with writing' : 'read only'}.
          </p>
        )}
        {(preview.data?.schedules.length ?? 0) > 0 && (
          <p className="mt-1 text-xs text-secondary">{preview.data!.schedules.length} briefing schedules, every one paused until you start it.</p>
        )}
        {(preview.data?.existing.length ?? 0) > 0 && (
          <p className="mt-1 text-xs text-tertiary">From this set already: {preview.data!.existing.map((d) => d.name).join(', ')}. The same name uses that department again.</p>
        )}
      </div>

      {consentItems > 0 && (
        <div className="mb-4 rounded-card border border-subtle p-3 text-xs" data-testid="set-consent">
          <label className="flex items-start gap-2 cursor-pointer">
            <input type="checkbox" checked={approve} onChange={(e) => setApprove(e.target.checked)} className="mt-0.5 accent-action" data-testid="set-consent-checkbox" />
            <span className="text-primary">
              {preview.data?.consent_scope === 'everyone'
                ? 'Approve the apps and checks of every agent for everyone who gets a copy.'
                : 'Approve the apps and checks of every agent: the shared ones and your own copies.'}
              {' '}<span className="text-tertiary">{consentItems} in all.</span>
            </span>
          </label>
          <p className="mt-1 text-tertiary">Unticked, each app waits for approval on its card and each check is offered, not mandatory.</p>
        </div>
      )}

      {error && <p className="text-xs text-status-danger-fg mb-3">{error}</p>}
      {preview.data?.running_job && <p className="text-xs text-status-warning-fg mb-3">This set is being installed already. Wait for it to finish.</p>}

      <div className="flex justify-end gap-2">
        <Button size="md" onClick={onClose}>Cancel</Button>
        <Button variant="primary" size="md" onClick={submit}
                disabled={running || !name.trim() || Boolean(preview.data?.name_problem) || blocked.length > 0 || members.length === 0}>
          {install.isPending ? 'Starting…' : 'Install the department'}
        </Button>
      </div>
    </Dialog>
  )
}
