import { useState } from 'react'
import {
  useAgentApiKeys,
  useCreateAgentApiKey,
  useRevokeAgentApiKey,
  type CreatedAgentApiKey,
} from '../../api/agentApiKeys'
import { Modal, Field, FormError } from './AgentTriggers.modals'
import { errorText, toastError } from '../../lib/http/apiJson'
import { useDialogs } from '../../components/ui/dialogs'
import Button from '../../components/ui/Button'
import { Input } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE, CARD, BUTTON_DANGER_QUIET } from '../../components/ui/classes'


// =====================================================================
// Agent API Keys section (managers only)
// =====================================================================

export function AgentApiKeysSection({ agent }: { agent: string }) {
  const dialogs = useDialogs()
  const { data: keys = [], isLoading } = useAgentApiKeys(agent)
  const createM = useCreateAgentApiKey()
  const revokeM = useRevokeAgentApiKey()
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [createdKey, setCreatedKey] = useState<CreatedAgentApiKey | null>(null)
  const [createError, setCreateError] = useState('')

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    setCreateError('')
    if (!newName.trim()) {
      setCreateError('Give the key a name.')
      return
    }
    try {
      const created = await createM.mutateAsync({ agent, name: newName.trim(), permissions: ['triggers'] })
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
    await revokeM.mutateAsync({ agent, keyId: id }).catch(toastError('The key was not revoked'))
  }

  return (
    <div className="space-y-3 border-t border-subtle pt-6 mt-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold font-display text-primary">Agent API Keys</h2>
          <p className="text-xs text-secondary mt-1">
            Keys that let other systems fire this agent's webhook triggers.
            They send a key as <code>Authorization: Bearer otok_…</code>.
            The master <code>PROXY_API_KEY</code> does not work on webhook URLs.
          </p>
        </div>
        <Button variant="primary" onClick={() => { setCreateError(''); setShowCreate(true) }} className="self-start sm:self-auto shrink-0">
          + Create key
        </Button>
      </div>

      {isLoading ? (
        <p className="text-sm text-secondary">Loading...</p>
      ) : keys.length === 0 ? (
        <p className="text-sm text-tertiary">No API keys for this agent yet.</p>
      ) : (
        <div className={`${CARD} divide-y divide-subtle`}>
          {keys.map((k) => (
            <div key={k.id} className="p-3 flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-primary">{k.name}</span>
                  <span className="text-xs font-mono text-secondary">otok_{k.prefix}…</span>
                  {k.permissions.map((p) => (
                    <span key={p} className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>{p}</span>
                  ))}
                  {k.revoked_at && <span className={`${BADGE_BASE} ${BADGE_TONE.danger}`}>revoked</span>}
                </div>
                <p className="text-xs text-secondary mt-0.5">
                  Created {new Date(k.created_at).toLocaleDateString()}
                  {k.last_used_at ? ` · last used ${new Date(k.last_used_at).toLocaleString()}` : ' · never used'}
                </p>
              </div>
              {!k.revoked_at && (
                <button type="button" className={BUTTON_DANGER_QUIET} onClick={() => onRevoke(k.id, k.name)}>
                  Revoke
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {showCreate && (
        <Modal onClose={() => setShowCreate(false)} title={`Create API key for ${agent}`} busy={createM.isPending}>
          <form onSubmit={onCreate} className="space-y-3">
            <Field label="Name">
              <Input
                autoFocus
                aria-label="Name"
                value={newName}
                onChange={(e) => { setNewName(e.target.value); setCreateError('') }}
                placeholder="e.g. Uptime monitor"
              />
            </Field>
            <Field label="Permissions">
              <p className="text-sm text-secondary">
                <code>triggers</code>: fire this agent's webhook triggers
              </p>
            </Field>
            <FormError text={createError} />
            <div className="flex justify-end gap-2 pt-2">
              <Button onClick={() => setShowCreate(false)} disabled={createM.isPending}>Cancel</Button>
              <Button type="submit" variant="primary" disabled={createM.isPending}>
                {createM.isPending ? 'Creating…' : 'Create'}
              </Button>
            </div>
          </form>
        </Modal>
      )}

      {createdKey && (
        <Modal onClose={() => setCreatedKey(null)} title="API key created" dismissible="escape">
          <p className="text-sm text-status-warning-fg bg-status-warning-bg border border-status-warning-border rounded-card p-2 mb-3">
            <span className="font-medium">Copy this key now.</span> It won't be shown again.
          </p>
          <div className="bg-surface-sunken rounded-sm p-2 mb-3 font-mono text-xs break-all">{createdKey.key}</div>
          <div className="flex justify-end gap-2">
            <Button variant="primary" onClick={onCopy}>Copy</Button>
            <Button onClick={() => setCreatedKey(null)}>Done</Button>
          </div>
        </Modal>
      )}
    </div>
  )
}
