import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  useMyIntegrations,
} from '../api/credentials'
import { UserAccountsManager } from '../components/UserAccountsManager'
import {
  useUserApiKeys,
  useCreateUserApiKey,
  useRevokeUserApiKey,
  type CreatedUserApiKey,
} from '../api/userApiKeys'
import { errorText, toastError } from '../lib/http/apiJson'
import { useDialogs } from '../components/ui/dialogs'
import Dialog from '../components/ui/Dialog'
import Button from '../components/ui/Button'
import { Input, Label } from '../components/ui/Field'
import { BUTTON_DANGER_QUIET, CARD } from '../components/ui/classes'
import { Badge } from '../components/ui/SettingsControls'
import InfoTip from '../components/ui/InfoTip'

function ApiKeysSection() {
  const dialogs = useDialogs()
  const { data: keys = [], isLoading } = useUserApiKeys()
  const createM = useCreateUserApiKey()
  const revokeM = useRevokeUserApiKey()
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [perms, setPerms] = useState<{ triggers: boolean }>({ triggers: true })
  const [createdKey, setCreatedKey] = useState<CreatedUserApiKey | null>(null)
  const [createError, setCreateError] = useState('')

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    setCreateError('')
    if (!newName.trim()) {
      setCreateError('Give the key a name.')
      return
    }
    const permList: string[] = []
    if (perms.triggers) permList.push('triggers')
    if (permList.length === 0) {
      setCreateError('Pick at least one permission.')
      return
    }
    try {
      const created = await createM.mutateAsync({ name: newName.trim(), permissions: permList })
      setCreatedKey(created)
      setNewName('')
      setShowCreate(false)
    } catch (e) {
      setCreateError(errorText(e) || 'The key was not created.')
    }
  }

  const onCopy = async () => {
    if (!createdKey) return
    try { await navigator.clipboard.writeText(createdKey.key) } catch { /* ignore */ }
  }

  const onRevoke = async (id: string, name: string) => {
    if (!(await dialogs.confirm({
      title: `Revoke the key "${name}"?`,
      body: 'Webhooks using it fail at once.',
      confirmLabel: 'Revoke',
      tone: 'danger',
    }))) return
    await revokeM.mutateAsync(id).catch(toastError('The key was not revoked'))
  }

  return (
    <div className="mb-8">
      <h2 className="font-display text-lg font-semibold text-primary mb-3">API Keys</h2>
      <p className="text-sm text-secondary mb-4">
        Personal API keys for webhook triggers. External systems authenticate to
        your user-scoped trigger webhooks with these.
      </p>

      {isLoading ? (
        <p className="text-sm text-secondary">Loading...</p>
      ) : (
        <div className="space-y-2">
          {keys.length === 0 && (
            <p className="text-sm text-secondary">No API keys yet.</p>
          )}
          {keys.map((k) => (
            <div key={k.id} className={`${CARD} p-3 flex items-center gap-3`}>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-primary">{k.name}</span>
                  {k.permissions.map((p) => (
                    <Badge key={p}>{p}</Badge>
                  ))}
                  {k.revoked_at && (
                    <Badge variant="danger">revoked</Badge>
                  )}
                </div>
                <p className="text-xs text-secondary mt-0.5">
                  Created {new Date(k.created_at).toLocaleDateString()}
                  {k.last_used_at ? ` · last used ${new Date(k.last_used_at).toLocaleString()}` : ' · never used'}
                </p>
              </div>
              {!k.revoked_at && (
                <button
                  onClick={() => onRevoke(k.id, k.name)}
                  className={BUTTON_DANGER_QUIET}
                >
                  Revoke
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <Button variant="primary" onClick={() => { setCreateError(''); setShowCreate(true) }} className="mt-3">
        + Create key
      </Button>

      {showCreate && (
        <Dialog title="Create API key" onClose={() => setShowCreate(false)} busy={createM.isPending}>
            <form onSubmit={onCreate} className="space-y-3 p-4">
              <div>
                <Label htmlFor="user-key-name">Name</Label>
                <Input
                  id="user-key-name"
                  autoFocus
                  value={newName}
                  onChange={(e) => { setNewName(e.target.value); setCreateError('') }}
                  placeholder="e.g. GitHub webhook"
                />
              </div>
              <div>
                <div className="mb-1">
                  <label className="text-xs font-medium text-primary">Permissions</label>
                  <InfoTip label="Permissions">More permissions are coming soon, like chat, tasks and notifications.</InfoTip>
                </div>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={perms.triggers}
                    onChange={(e) => setPerms({ ...perms, triggers: e.target.checked })}
                    className="accent-action"
                  />
                  <span><code>triggers</code>: fire your own webhook triggers</span>
                </label>
              </div>
              {createError && <p role="alert" className="text-xs text-status-danger-fg">{createError}</p>}
              <div className="flex justify-end gap-2 pt-2">
                <Button onClick={() => setShowCreate(false)} disabled={createM.isPending}>Cancel</Button>
                <Button type="submit" variant="primary" disabled={createM.isPending}>
                  {createM.isPending ? 'Creating…' : 'Create'}
                </Button>
              </div>
            </form>
        </Dialog>
      )}

      {createdKey && (
        // The key is shown once: only Escape or Done closes it, never a stray click.
        <Dialog title="API key created" onClose={() => setCreatedKey(null)} dismissible="escape">
          <div className="p-4">
            <p className="text-sm text-status-warning-fg bg-status-warning-bg rounded-card p-2 mb-3">
              <span className="font-medium">Copy this key now.</span> It will not be shown again. If you lose it, revoke it and create a new one.
            </p>
            <div className="bg-surface-sunken text-primary rounded-card p-2 mb-3 font-mono text-xs break-all">{createdKey.key}</div>
            <div className="flex justify-end gap-2">
              <Button variant="primary" onClick={onCopy}>Copy to clipboard</Button>
              <Button onClick={() => setCreatedKey(null)}>Done</Button>
            </div>
          </div>
        </Dialog>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Integrations Tab (connected MCP accounts + personal trigger API keys)
// ---------------------------------------------------------------------------

export function IntegrationsTab() {
  const { data: integrations, isLoading } = useMyIntegrations()
  // `?provider=<mcp name>` (an app's "Connect X" button, navigation by kind)
  // scrolls to that integration's row and lights it for a moment. Only a
  // click on the row starts a connect flow — never the arrival itself.
  const [searchParams] = useSearchParams()
  const provider = searchParams.get('provider') || ''
  const [highlightDone, setHighlightDone] = useState('')
  const highlight = provider && highlightDone !== provider && integrations?.some((i) => i.mcp_name === provider)
    ? provider : ''
  useEffect(() => {
    if (!highlight) return
    document.getElementById(`integration-${highlight}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    const timer = setTimeout(() => setHighlightDone(highlight), 2500)
    return () => clearTimeout(timer)
  }, [highlight])
  return (
    <div>
      <div className="mb-8">
        <h2 className="font-display text-lg font-semibold text-primary mb-3">Connected Accounts</h2>
        <p className="text-sm text-secondary mb-4">
          Configure your personal credentials for MCP tools. These are used when agents access external services on your behalf.
        </p>
        {isLoading ? (
          <div className="text-sm text-tertiary">Loading...</div>
        ) : integrations && integrations.length > 0 ? (
          <div className="space-y-3">
            {integrations.map(i => (
              <div
                key={i.mcp_name}
                id={`integration-${i.mcp_name}`}
                className={`rounded-card transition-shadow ${highlight === i.mcp_name ? 'ring-2 ring-focus-ring ring-offset-2 ring-offset-surface-page' : ''}`}
              >
                <UserAccountsManager integration={i} />
              </div>
            ))}
          </div>
        ) : (
          <div className={`${CARD} text-sm text-tertiary p-6 text-center`}>
            No integrations available for your assigned agents.
          </div>
        )}
      </div>
      <ApiKeysSection />
    </div>
  )
}
