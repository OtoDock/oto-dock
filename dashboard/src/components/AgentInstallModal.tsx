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

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useCreateAgent } from '../api/agents'
import { useAuth } from '../contexts/AuthContext'
import {
  type VisibilityMode,
  columnsOf,
  MODE_LABEL,
  MODE_OPTION_HINT,
  MODE_GROUPS,
} from '../lib/visibility'
import {
  CommunityAgentRegistryEntry,
  useInstallCommunityAgent,
  useInstallPreview,
} from '../api/communityAgents'

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
  // Easily changed afterward in the agent's Configuration tab.
  const [visibilityMode, setVisibilityMode] = useState<VisibilityMode>('personal_shared')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
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
    setError('')
  }, [open, mode, template])

  // Cascade preview (install mode only).
  const preview = useInstallPreview(
    mode === 'install' ? template?.slug ?? null : null,
    mode === 'install' ? slug || null : null,
  )

  const installing = createAgent.isPending || installAgent.isPending

  // MCPs the server's preflight would hard-reject (installed nowhere, in no
  // catalog) — installing is pointless, the POST 400s. Gate the button on a
  // POSITIVE finding only: a loading/failed preview must not block install
  // (the server-side preflight still guards).
  const blockedMcps =
    mode === 'install' ? (preview.data?.required_mcps.filter(m => m.blocked) ?? []) : []

  const handleSubmit = async () => {
    setError('')
    const cleanSlug = slug.trim()
    const cleanName = displayName.trim()
    if (!cleanName) {
      setError('Display name is required')
      return
    }
    if (!cleanSlug) {
      setError('Slug is required')
      return
    }
    if (!/^[a-z][a-z0-9-]{1,38}[a-z0-9]$/.test(cleanSlug)) {
      setError('Slug must be lowercase alphanumeric with hyphens (3-40 chars)')
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
        const result = await installAgent.mutateAsync({
          template_slug: template!.slug,
          target_slug: attemptSlug,
        })
        onClose()
        navigate(`/agents/${result.agent_slug}/config`)
        return
      } catch (err: any) {
        if (err.status === 409 && err.body?.detail?.suggested_slug) {
          attemptSlug = err.body.detail.suggested_slug
          setSlug(attemptSlug)
          continue
        }
        setError(err.body?.detail?.message || err.message || 'Install failed')
        return
      }
    }
    setError(`Couldn't find a free slug — try editing the slug manually.`)
  }

  if (!open) return null

  const title = mode === 'install' ? 'Install community agent' : 'Create new agent'

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs"
      onClick={onClose}
    >
      <div
        className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light shadow-xl p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        <h3 className="text-sm font-semibold text-p-text mb-4">{title}</h3>

        {/* Install-mode banner */}
        {mode === 'install' && template && (
          <div className="mb-4 rounded-lg border border-p-border-light bg-p-bg p-3 flex items-start gap-3">
            <div
              className="w-10 h-10 rounded-lg shrink-0 flex items-center justify-center text-white font-semibold"
              style={{ background: template.color }}
            >
              {template.display_name.charAt(0)}
            </div>
            <div className="min-w-0">
              <div className="text-sm font-medium text-p-text">
                {template.display_name} <span className="text-xs text-p-text-secondary">v{template.version}</span>
              </div>
              <div className="text-xs text-p-text-secondary">by {template.author}</div>
              <div className="text-xs text-p-text-secondary mt-1 line-clamp-2">{template.description}</div>
            </div>
          </div>
        )}

        {/* Form */}
        <label className="block text-xs font-medium text-p-text-secondary mb-1">Display name</label>
        <input
          type="text"
          value={displayName}
          onChange={e => {
            setDisplayName(e.target.value)
            if (!slugEdited && mode === 'create') {
              setSlug(slugify(e.target.value))
            }
          }}
          placeholder="e.g. Sales Assistant"
          className="w-full px-3 py-2 rounded-lg border border-p-border-light bg-white dark:bg-p-bg text-sm text-p-text placeholder:text-p-text-light focus:outline-hidden focus:ring-2 focus:ring-brand/40 mb-3"
          autoFocus
        />

        <label className="block text-xs font-medium text-p-text-secondary mb-1">Slug</label>
        <input
          type="text"
          value={slug}
          onChange={e => {
            setSlug(e.target.value)
            setSlugEdited(true)
          }}
          placeholder="e.g. sales-assistant"
          className="w-full px-3 py-2 rounded-lg border border-p-border-light bg-white dark:bg-p-bg text-sm text-p-text font-mono placeholder:text-p-text-light focus:outline-hidden focus:ring-2 focus:ring-brand/40 mb-3"
        />

        {/* Create-mode options */}
        {mode === 'create' && (
          <div className="flex flex-col gap-3 mb-4">
            <div>
              <label className="block text-xs font-medium text-p-text-secondary mb-1">Visibility &amp; workspace</label>
              <select
                value={visibilityMode}
                onChange={e => setVisibilityMode(e.target.value as VisibilityMode)}
                className="w-full px-3 py-2 rounded-lg border border-p-border-light bg-white dark:bg-p-bg text-sm text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/40"
              >
                {MODE_GROUPS.map(group => (
                  <optgroup key={group.label} label={group.label}>
                    {group.modes.map(m => (
                      <option key={m} value={m}>{MODE_LABEL[m]}</option>
                    ))}
                  </optgroup>
                ))}
              </select>
              <p className="text-xs text-p-text-light mt-1">{MODE_OPTION_HINT[visibilityMode]}</p>
            </div>
            {user?.role === 'admin' && (
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={adminOnly}
                  onChange={e => setAdminOnly(e.target.checked)}
                  className="rounded-sm border-p-border-light text-brand focus:ring-brand/40"
                />
                <span className="text-sm text-p-text-secondary">Admin only</span>
              </label>
            )}
          </div>
        )}

        {/* Install-mode cascade preview */}
        {mode === 'install' && template && (
          <div className="mb-4 rounded-lg border border-p-border-light bg-p-bg p-3">
            <div className="text-xs font-medium text-p-text mb-2">Required MCPs</div>
            {preview.isLoading && (
              <div className="text-xs text-p-text-secondary">Checking platform state…</div>
            )}
            {preview.data && (
              <>
                {!preview.data.slug_available && preview.data.suggested_slug && (
                  <div className="mb-2 text-xs text-amber-600 dark:text-amber-400">
                    Slug taken — server will use <code className="font-mono">{preview.data.suggested_slug}</code>.
                  </div>
                )}
                <ul className="space-y-1 max-h-48 overflow-y-auto">
                  {preview.data.required_mcps.map(m => (
                    <li key={m.name} className="text-xs flex items-start gap-2">
                      <span className={
                        !m.needs_request
                          ? 'text-green-600 dark:text-green-400'
                          : m.blocked
                            ? 'text-red-600 dark:text-red-400'
                            : 'text-amber-600 dark:text-amber-400'
                      }>
                        {!m.needs_request ? '✓' : m.blocked ? '✗' : '⏳'}
                      </span>
                      <span className="font-mono">{m.name}</span>
                      <span className="text-p-text-secondary truncate">— {m.reason}</span>
                    </li>
                  ))}
                </ul>
                <div className="mt-2 text-xs text-p-text-secondary">
                  {preview.data.required_mcps.filter(m => !m.needs_request).length} ready,{' '}
                  {preview.data.required_mcps.filter(m => m.needs_request && !m.blocked).length} need admin approval,{' '}
                  {preview.data.required_mcps.filter(m => m.blocked).length} blocked
                  {preview.data.will_create_tasks_agent_scope > 0 && (
                    <> · {preview.data.will_create_tasks_agent_scope} tasks will be seeded</>
                  )}
                </div>
                {blockedMcps.length > 0 && (
                  <div className="mt-2 text-xs text-red-600 dark:text-red-400">
                    {blockedMcps.length === 1
                      ? `${blockedMcps[0].name} is not installed and not in any catalog`
                      : `${blockedMcps.length} required MCPs are not installed and not in any catalog`}
                    {' '}— this template can't be installed on this platform.
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* Error */}
        {error && <p className="text-xs text-red-500 mb-3">{error}</p>}

        {/* Actions */}
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-sm font-medium text-p-text-secondary bg-p-surface hover:bg-p-surface-hover transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={installing || blockedMcps.length > 0}
            className="px-4 py-2 rounded-lg text-sm font-medium text-white bg-brand hover:bg-brand-hover transition-colors disabled:opacity-50"
          >
            {installing
              ? mode === 'install' ? 'Installing…' : 'Creating…'
              : mode === 'install' ? 'Install agent' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  )
}
