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
import { DetailsToggle, ManifestBlocks, ManifestSummary } from './apps/AppApprovalCard'
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
}

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

export default function AgentInstallModal({ open, mode, template, onClose }: Props) {
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
  const [installed, setInstalled] = useState<{ slug: string; notes: string[] } | null>(null)
  // The consent (COMMUNITY-AGENTS-REGISTRY.md "Consent"): approve the
  // template's apps and checks at install — for every member's copy when
  // an admin installs, for the shared apps and one's own copy otherwise.
  // A copy whose manifest does things only its owner may approve always
  // waits on that owner's card; the dialog says so per app.
  const [approveAll, setApproveAll] = useState(true)
  const [openDetails, setOpenDetails] = useState<Record<string, boolean>>({})

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
      setOpenDetails({})
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
            }
          : {}
        const result = await installAgent.mutateAsync({
          template_slug: template!.slug,
          target_slug: attemptSlug,
          display_name: cleanName,
          ...consent,
        })
        const notes = taskPinNotes(result)
        if (notes.length > 0) {
          setInstalled({ slug: result.agent_slug, notes })
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
        <div className="text-xs mb-4" data-testid="install-pins-dropped">
          <div className="font-medium text-primary mb-1">{TASK_PINS_HEADING}</div>
          <ul className="list-disc pl-5 space-y-0.5 text-secondary">
            {installed.notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </div>
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

  return (
    <Dialog
      onClose={onClose}
      busy={installing}
      aria-labelledby="agent-install-title"
      panelClassName={`${DIALOG_PANEL} p-6 max-w-lg max-h-[90vh] overflow-y-auto`}
    >
        <h3 id="agent-install-title" className="text-sm font-semibold text-primary mb-4">{title}</h3>

        {/* Install-mode banner */}
        {mode === 'install' && template && (
          <div className="mb-4 rounded-card border border-subtle p-3 flex items-start gap-3">
            <div
              // eslint-disable-next-line otodock/no-raw-palette -- white initial on the template's own colour
              className="w-10 h-10 rounded-card shrink-0 flex items-center justify-center text-white font-semibold"
              style={{ background: template.color }}
            >
              {template.display_name.charAt(0)}
            </div>
            <div className="min-w-0">
              <div className="text-sm font-medium text-primary">
                {template.display_name} <span className="text-xs text-secondary">v{template.version}</span>
              </div>
              <div className="text-xs text-secondary">by {template.author}</div>
              <div className="text-xs text-secondary mt-1 line-clamp-2">{template.description}</div>
            </div>
          </div>
        )}

        {/* Form */}
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

        {/* Install-mode cascade preview */}
        {mode === 'install' && template && (
          <div className="mb-4 rounded-card border border-subtle p-3">
            <div className="text-xs font-medium text-primary mb-2">Required MCPs</div>
            {preview.isLoading && (
              <div className="text-xs text-secondary">Checking platform state…</div>
            )}
            {preview.data && (
              <>
                {!preview.data.slug_available && preview.data.suggested_slug && (
                  <div className="mb-2 text-xs text-status-warning-fg">
                    That ID is taken. OtoDock will use <code className="font-mono">{preview.data.suggested_slug}</code>.
                  </div>
                )}
                <ul className="space-y-1 max-h-48 overflow-y-auto">
                  {preview.data.required_mcps.map(m => (
                    <li key={m.name} className="text-xs flex items-start gap-2">
                      <span className={
                        !m.needs_request
                          ? 'text-status-success-fg'
                          : m.blocked
                            ? 'text-status-danger-fg'
                            : 'text-status-warning-fg'
                      }>
                        {!m.needs_request ? '✓' : m.blocked ? '✗' : '⏳'}
                      </span>
                      <span className="font-mono">{m.name}</span>
                      <span className="text-secondary truncate">· {m.reason}</span>
                    </li>
                  ))}
                </ul>
                <div className="mt-2 text-xs text-secondary">
                  {preview.data.required_mcps.filter(m => !m.needs_request).length} ready,{' '}
                  {preview.data.required_mcps.filter(m => m.needs_request && !m.blocked).length} need admin approval,{' '}
                  {preview.data.required_mcps.filter(m => m.blocked).length} blocked
                  {preview.data.will_create_tasks_agent_scope > 0 && (
                    <> · {preview.data.will_create_tasks_agent_scope} tasks will be seeded</>
                  )}
                </div>
                {blockedMcps.length > 0 && (
                  <div className="mt-2 text-xs text-status-danger-fg">
                    {blockedMcps.length === 1
                      ? `${blockedMcps[0].name}: ${blockedMcps[0].reason || 'not installed and not in the community list'}.`
                      : `${blockedMcps.length} required MCPs are blocked. Each is not installed and not in the community list, or has no instance.`}
                    {' '}This template can't be installed here.
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* The apps and checks the template ships, and the consent */}
        {mode === 'install' && hasConsentItems && (
          <div className="mb-4 rounded-card border border-subtle p-3 text-xs" data-testid="install-consent">
            {previewApps.length > 0 && (
              <div className="font-medium text-primary mb-2">
                {previewApps.length === 1 ? 'An app it brings' : `${previewApps.length} apps it brings`}
              </div>
            )}
            {previewApps.map(a => (
              <div key={a.slug} className="mb-3" data-testid={`consent-app-${a.slug}`}>
                <div className="flex flex-wrap items-baseline gap-x-1.5">
                  <span className="font-medium text-primary">{a.title}</span>
                  <span className="text-secondary">
                    {a.visibility === 'user' ? 'one copy for each member' : 'shared by the agent'}
                    {a.owner_approval ? ' · each member approves their own copy' : ''}
                  </span>
                </div>
                <ManifestSummary app={a.row} />
                {a.blueprint_tasks.length > 0 && (
                  <details className="mt-1 text-secondary">
                    <summary className="cursor-pointer">The tasks its buttons run</summary>
                    <ul className="mt-1 space-y-1 pl-3">
                      {a.blueprint_tasks.map(t => (
                        <li key={t.slug}>
                          <span className="font-medium text-primary">{t.description || t.slug}</span>
                          <span className="block break-words">“{t.prompt.slice(0, 300)}{t.prompt.length > 300 ? '…' : ''}”</span>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
                {(a.blueprint_triggers?.length ?? 0) > 0 && (
                  <p className="mt-1 text-secondary" data-testid={`consent-app-triggers-${a.slug}`}>
                    Comes with {a.blueprint_triggers!.length === 1 ? 'a trigger' : `${a.blueprint_triggers!.length} triggers`}
                    {a.visibility === 'user' ? ' of each member’s own' : ''}: {a.blueprint_triggers!.map(t => t.description || t.slug).join(', ')}
                  </p>
                )}
                <div className="mt-1">
                  <DetailsToggle
                    open={!!openDetails[a.slug]}
                    onToggle={() => setOpenDetails(d => ({ ...d, [a.slug]: !d[a.slug] }))}
                  />
                </div>
                {openDetails[a.slug] && <ManifestBlocks app={a.row} />}
              </div>
            ))}
            {previewChecks.length > 0 && (
              <div className="mb-2">
                <div className="font-medium text-primary mb-1">
                  {previewChecks.length === 1 ? 'A check it brings' : `${previewChecks.length} checks it brings`}
                </div>
                <ul className="space-y-1 text-secondary">
                  {previewChecks.map(c => (
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
                data-testid="consent-checkbox"
              />
              <span className="text-primary">
                {consentScope === 'everyone'
                  ? 'Approve its apps and checks for everyone who gets a copy'
                  : 'Approve its apps and checks: the shared ones and your own copy. Members approve theirs.'}
              </span>
            </label>
            <p className="mt-1 text-tertiary">
              {approveAll
                ? 'Unticked, each app waits for approval on its card. Each check is offered, not mandatory.'
                : 'Each app will wait for approval on its card. Each check will be offered, not mandatory.'}
            </p>
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
