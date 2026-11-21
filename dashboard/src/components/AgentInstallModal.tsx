/**
 * Unified Agent Install / Create modal.
 *
 * Two modes:
 *
 * - ``mode="create"`` — empty fields, behaves like the legacy "Create New
 *   Agent" modal. Calls ``useCreateAgent``.
 *
 * - ``mode="install"`` — pre-fills from a community-agent template,
 *   shows the cascade preview ("X MCPs ready, Y need admin approval"),
 *   and calls ``useInstallCommunityAgent`` on submit. Handles slug
 *   collision via the server's ``suggested_slug`` response and retries
 *   silently up to 3 times.
 */

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useCreateAgent } from '../api/agents'
import { useAuth } from '../contexts/AuthContext'
import { isAdmin } from '../lib/permissions'
import {
  type VisibilityMode,
  columnsOf,
  MODE_LABEL,
  MODE_OPTION_HINT,
  MODE_GROUPS,
} from '../lib/visibility'
import {
  CommunityAgentRegistryEntry,
  type PreviewApp,
  taskPinNotes,
  TASK_PINS_HEADING,
  useInstallCommunityAgent,
  useInstallPreview,
} from '../api/communityAgents'
import RequiredTools from './RequiredTools'
import TemplateApps from './common/TemplateApps'
import { memberOfLine } from '../lib/status/departmentSet'
import TemplatePanel from './common/TemplatePanel'
import { errorText, refusalProblems } from '../lib/http/apiJson'
import { isRecord } from '../lib/json/guards'
import Dialog from './ui/Dialog'
import Button from './ui/Button'
import { Input, Select } from './ui/Field'
import { DIALOG_PANEL } from './ui/classes'

interface Props {
  open: boolean
  mode: 'create' | 'install'
  template?: CommunityAgentRegistryEntry | null
  onClose: () => void
  /** The department the template belongs to, with its own install. */
  memberOf?: { name: string; onInstall: () => void } | null
}

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

export default function AgentInstallModal({ open, mode, template, onClose, memberOf }: Props) {
  const { user } = useAuth()
  const navigate = useNavigate()
  const createAgent = useCreateAgent()
  const installAgent = useInstallCommunityAgent()

  const initialSlug = mode === 'install' && template ? template.slug : ''
  const initialName = mode === 'install' && template ? template.display_name : ''

  const [displayName, setDisplayName] = useState(initialName)
  const [slug, setSlug] = useState(initialSlug)
  const [slugEdited, setSlugEdited] = useState(false)
  const [adminOnly, setAdminOnly] = useState(false)
  // Visibility mode for a freshly-created agent. Default = Personal + shared.
  // Easily changed afterward in the agent's Configuration section.
  const [visibilityMode, setVisibilityMode] = useState<VisibilityMode>('personal_shared')
  const [error, setError] = useState('')
  const [problems, setProblems] = useState<string[]>([])
  // An install whose tasks lost their model choice holds the dialog on what
  // it did, so the notes are read before the agent's page opens.
  const [installed, setInstalled] = useState<{ slug: string; pins: string[]; department: string[] } | null>(null)
  // The consent (COMMUNITY-AGENTS-REGISTRY.md "Consent"): approve the
  // template's apps and checks at install — for every member's copy when
  // an admin installs, for the shared apps and one's own copy otherwise.
  // A copy whose manifest does things only its owner may approve always
  // waits on that owner's card; the dialog says so per app.
  const [approveAll, setApproveAll] = useState(true)
  // The offer (the proxy's department_set_installer.offer_for): the template belongs to a set whose
  // other members this person holds as standalone agents.
  const [takeOffer, setTakeOffer] = useState(true)
  const [offerName, setOfferName] = useState('')

  // The dialog opening, or its template changing while open, starts the
  // form over (in render, on the transition).
  const [openedFor, setOpenedFor] = useState({ open, mode, template })
  if (openedFor.open !== open || openedFor.mode !== mode || openedFor.template !== template) {
    setOpenedFor({ open, mode, template })
    if (open) {
      if (mode === 'install' && template) {
        setDisplayName(template.display_name)
        setSlug(template.slug)
      } else {
        setDisplayName('')
        setSlug('')
      }
      setSlugEdited(false)
      setAdminOnly(false)
      setVisibilityMode('personal_shared')
      setApproveAll(true)
      setTakeOffer(true)
      setOfferName('')
      setError('')
      setProblems([])
      setInstalled(null)
    }
  }

  // Cascade preview (install mode only).
  const preview = useInstallPreview(
    mode === 'install' ? template?.slug ?? null : null,
    mode === 'install' ? slug || null : null,
  )
  const previewApps: PreviewApp[] = mode === 'install' ? (preview.data?.apps ?? []) : []
  const previewChecks = mode === 'install' ? (preview.data?.checks ?? []) : []
  const consentScope = preview.data?.consent_scope ?? 'everyone'
  const hasConsentItems = previewApps.length > 0 || previewChecks.length > 0
  const offer = mode === 'install' ? preview.data?.set ?? null : null
  const offerDepartmentName = offerName || offer?.name || ''

  const installing = createAgent.isPending || installAgent.isPending

  // MCPs the server's preflight would hard-reject (installed nowhere, in no
  // catalog) — installing is pointless, the POST 400s. Gate the button on a
  // POSITIVE finding only: a loading/failed preview must not block install
  // (the server-side preflight still guards).
  const blockedMcps =
    mode === 'install' ? (preview.data?.required_mcps.filter(m => m.blocked) ?? []) : []

  const openAgent = (agentSlug: string) => {
    onClose()
    navigate(`/agents/${agentSlug}/config`)
  }

  const handleSubmit = async () => {
    setError('')
    setProblems([])
    const cleanSlug = slug.trim()
    const cleanName = displayName.trim()
    if (!cleanName) {
      setError('Display name is required')
      return
    }
    if (!cleanSlug) {
      setError('ID is required')
      return
    }
    if (!/^[a-z][a-z0-9-]{1,38}[a-z0-9]$/.test(cleanSlug)) {
      setError('The ID must be 3 to 40 lowercase letters, digits or hyphens.')
      return
    }

    if (mode === 'create') {
      createAgent.mutate(
        { display_name: cleanName, slug: cleanSlug, admin_only: adminOnly, ...columnsOf(visibilityMode) },
        {
          onSuccess: () => {
            onClose()
            navigate(`/agents/${cleanSlug}/config`)
          },
          onError: (err: Error) => setError(err.message),
        },
      )
      return
    }

    // Install mode: retry on 409 slug-collision with the server's
    // suggested slug, up to 3 times, before giving up and surfacing the
    // error.
    let attemptSlug = cleanSlug
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const consent = approveAll && hasConsentItems
          ? {
              approve_apps: Object.fromEntries(previewApps.map(a => [a.slug, a.sig])),
              approve_checks: Object.fromEntries(previewChecks.map(c => [c.name, c.sig])),
              // The apps whose server this dialog showed: the consent covers it.
              approve_servers: previewApps.filter(a => a.adds_server).map(a => a.slug),
            }
          : {}
        const result = await installAgent.mutateAsync({
          template_slug: template!.slug,
          target_slug: attemptSlug,
          display_name: cleanName,
          ...consent,
          ...(offer && takeOffer ? { department_set: { slug: offer.slug, name: offerDepartmentName.trim() } } : {}),
        })
        const pins = taskPinNotes(result)
        const department = result.department_lines ?? []
        if (pins.length > 0 || department.length > 0) {
          setInstalled({ slug: result.agent_slug, pins, department })
          return
        }
        openAgent(result.agent_slug)
        return
      } catch (err) {
        // The install refusal carries the status and the parsed body
        // (api/communityAgents.ts `useInstallCommunityAgent`).
        const failure = isRecord(err) ? err : {}
        const detail = isRecord(failure.body) && isRecord(failure.body.detail) ? failure.body.detail : {}
        if (failure.status === 409 && typeof detail.suggested_slug === 'string' && detail.suggested_slug) {
          attemptSlug = detail.suggested_slug
          setSlug(attemptSlug)
          continue
        }
        setProblems(refusalProblems(err))
        setError((typeof detail.message === 'string' && detail.message) || errorText(err) || 'Install failed')
        return
      }
    }
    setError(`Couldn't find a free ID. Try editing the ID yourself.`)
  }

  if (!open) return null

  const title = mode === 'install' ? 'Install community agent' : 'Create new agent'

  if (installed) {
    return (
      <Dialog
        onClose={onClose}
        aria-labelledby="agent-install-title"
        panelClassName={`${DIALOG_PANEL} p-6 max-w-lg max-h-[90vh] overflow-y-auto`}
      >
        <h3 id="agent-install-title" className="text-sm font-semibold text-primary mb-3">Agent installed</h3>
        {installed.department.length > 0 && (
          <ul className="text-xs mb-4 space-y-0.5 text-secondary" data-testid="install-department">
            {installed.department.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        )}
        {installed.pins.length > 0 && (
          <div className="text-xs mb-4" data-testid="install-pins-dropped">
            <div className="font-medium text-primary mb-1">{TASK_PINS_HEADING}</div>
            <ul className="list-disc pl-5 space-y-0.5 text-secondary">
              {installed.pins.map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" size="md" onClick={onClose}>
            Close
          </Button>
          <Button variant="primary" size="md" onClick={() => openAgent(installed.slug)}>
            Open the agent
          </Button>
        </div>
      </Dialog>
    )
  }

  // The name and ID: the whole form in create mode, the install's own fields in its panel.
  const fields = (
    <>
      <label className="block text-xs font-medium text-secondary mb-1">Display name</label>
      <Input
        type="text"
        value={displayName}
        onChange={e => {
          setDisplayName(e.target.value)
          if (!slugEdited && mode === 'create') {
            setSlug(slugify(e.target.value))
          }
        }}
        placeholder="e.g. Sales Assistant"
        className="mb-3"
        autoFocus
      />

      <label className="block text-xs font-medium text-secondary mb-1">ID</label>
      <Input
        type="text"
        value={slug}
        onChange={e => {
          setSlug(e.target.value)
          setSlugEdited(true)
        }}
        placeholder="e.g. sales-assistant"
        className="font-mono mb-3"
      />

    </>
  )

  return (
    <Dialog
      onClose={onClose}
      busy={installing}
      aria-labelledby="agent-install-title"
      panelClassName={`${DIALOG_PANEL} p-6 max-w-lg max-h-[90vh] overflow-y-auto`}
    >
        <h3 id="agent-install-title" className="text-sm font-semibold text-primary mb-4">{title}</h3>

        {/* Create mode: the form alone */}
        {mode === 'create' && fields}

        {/* Create-mode options */}
        {mode === 'create' && (
          <div className="flex flex-col gap-3 mb-4">
            <div>
              <label className="block text-xs font-medium text-secondary mb-1">Visibility and workspace</label>
              <Select
                value={visibilityMode}
                onChange={e => setVisibilityMode(e.target.value as VisibilityMode)}
              >
                {MODE_GROUPS.map(group => (
                  <optgroup key={group.label} label={group.label}>
                    {group.modes.map(m => (
                      <option key={m} value={m}>{MODE_LABEL[m]}</option>
                    ))}
                  </optgroup>
                ))}
              </Select>
              <p className="text-xs text-tertiary mt-1">{MODE_OPTION_HINT[visibilityMode]}</p>
            </div>
            {isAdmin(user) && (
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={adminOnly}
                  onChange={e => setAdminOnly(e.target.checked)}
                  className="accent-action"
                />
                <span className="text-sm text-secondary">Admin only</span>
              </label>
            )}
          </div>
        )}

        {/* Install mode: the template's panel, the fields, the folded lines and the approval */}
        {mode === 'install' && template && (
          <div className="mb-4">
            <TemplatePanel
              kind="agent"
              variant="full"
              name={template.display_name}
              color={template.color}
              version={template.version}
              author={template.author}
              summary={template.summary}
              description={template.description}
              features={template.highlights ?? []}
              memberOf={memberOf && (
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs" data-testid="panel-member-of">
                  <span className="text-secondary">{memberOfLine(memberOf.name)}</span>
                  <Button size="xs" onClick={memberOf.onInstall}>Install the whole department</Button>
                </div>
              )}
              slot={(
                <div>
                  {fields}
                  {preview.data && !preview.data.slug_available && preview.data.suggested_slug && (
                    <div className="-mt-1 mb-1 text-xs text-status-warning-fg">
                      That ID is taken. OtoDock will use <code className="font-mono">{preview.data.suggested_slug}</code>.
                    </div>
                  )}
                </div>
              )}
              folded={(
                <>
                  {preview.isLoading && <div className="text-xs text-secondary">Checking the tools it needs…</div>}
                  {preview.data && (
                    <>
                      <RequiredTools tools={preview.data.required_mcps} />
                      {preview.data.will_create_tasks_agent_scope > 0 && (
                        <p className="text-xs text-secondary">
                          {preview.data.will_create_tasks_agent_scope === 1
                            ? 'The agent comes with 1 scheduled task.'
                            : `The agent comes with ${preview.data.will_create_tasks_agent_scope} scheduled tasks.`}
                        </p>
                      )}
                      {blockedMcps.length > 0 && (
                        <p className="text-xs text-status-danger-fg">This agent cannot be installed here.</p>
                      )}
                    </>
                  )}
                  {hasConsentItems && (
                    <div data-testid="install-consent">
                      <TemplateApps
                        apps={previewApps}
                        checks={previewChecks}
                        approval={{
                          checked: approveAll,
                          onChange: setApproveAll,
                          scope: consentScope,
                          testId: 'consent-checkbox',
                          hint: approveAll
                            ? 'Unticked, each app waits for approval on its card. Each check is offered, not mandatory.'
                            : 'Each app will wait for approval on its card. Each check will be offered, not mandatory.',
                        }}
                      />
                    </div>
                  )}
                </>
              )}
            />
          </div>
        )}

        {/* The offer: the set's department for this agent and the standalone members */}
        {mode === 'install' && offer && (
          <div className="mb-4 rounded-card border border-subtle p-3 text-xs" data-testid="install-offer">
            <label className="flex items-start gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={takeOffer}
                onChange={e => setTakeOffer(e.target.checked)}
                className="mt-0.5 accent-action"
                data-testid="install-offer-checkbox"
              />
              <span className="text-primary">
                {offer.members.length === 1
                  ? `Create the ${offer.name} department and add both agents to it?`
                  : `Create the ${offer.name} department and add all ${offer.members.length + 1} agents to it?`}
              </span>
            </label>
            <p className="mt-1 text-secondary">
              {offer.members.map(m => m.display_name).join(', ')} {offer.members.length === 1 ? 'is' : 'are'} installed from the same set and in no department. The head&apos;s library is shared with the team.
            </p>
            {takeOffer && (
              <div className="mt-2">
                <label className="block font-medium text-secondary mb-1" htmlFor="offer-dept-name">Department name</label>
                <Input id="offer-dept-name" type="text" value={offerDepartmentName} onChange={e => setOfferName(e.target.value)} />
              </div>
            )}
          </div>
        )}

        {/* Error: a refused template lists its problems apart. */}
        {problems.length > 0 ? (
          <div className="text-xs text-status-danger-fg mb-3" data-testid="install-problems">
            <p className="mb-1">This template cannot be installed:</p>
            <ul className="list-disc pl-5 space-y-0.5">
              {problems.map((p, i) => <li key={i}>{p}</li>)}
            </ul>
          </div>
        ) : error && <p className="text-xs text-status-danger-fg mb-3">{error}</p>}

        {/* Actions */}
        <div className="flex justify-end gap-2">
          <Button size="md" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="md"
            onClick={handleSubmit}
            disabled={installing || blockedMcps.length > 0}
          >
            {installing
              ? mode === 'install' ? 'Installing…' : 'Creating…'
              : mode === 'install'
                ? approveAll && hasConsentItems ? 'Install and approve' : 'Install agent'
                : 'Create'}
          </Button>
        </div>
    </Dialog>
  )
}
