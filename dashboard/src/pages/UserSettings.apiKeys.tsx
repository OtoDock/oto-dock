import { useState } from 'react'
import { passkeyConfirm } from '../api/webauthn'
import { startOidcConfirm } from '../api/auth'
import { useAgents } from '../api/agents'
import {
  KEY_CONFIRM,
  KeyConfirmRequired,
  useCreateUserApiKey,
  useRevokeUserApiKey,
  useUserApiKeys,
  type CreatedUserApiKey,
  type KeyRevoked,
} from '../api/userApiKeys'
import Button from '../components/ui/Button'
import Dialog from '../components/ui/Dialog'
import { Input, Label } from '../components/ui/Field'
import InfoTip from '../components/ui/InfoTip'
import { Badge } from '../components/ui/SettingsControls'
import { BUTTON_DANGER_QUIET, CARD } from '../components/ui/classes'
import { useDialogs } from '../components/ui/dialogs'
import { useAuth } from '../contexts/AuthContext'
import { copyOrSay } from '../lib/copyText'
import { errorText, toastError } from '../lib/http/apiJson'
import { takeConfirm } from '../lib/shareConfirm'

const SCOPES: Array<{ id: string; label: string }> = [
  { id: 'chat', label: 'Send messages and read your chats' },
  { id: 'tasks', label: 'Run your tasks and read their results' },
  { id: 'files', label: 'Read and write your files' },
  { id: 'notifications', label: 'Send you notifications' },
  { id: 'triggers', label: 'Fire your own webhook triggers' },
]
const EXPIRY_DAYS = [7, 30, 90, 365]
/** A task's run has nobody to ask: the server takes it only with `unasked`. */
const TASKS = 'tasks'
const RETURN_TO = '/user-settings/integrations'

type KeyForm = {
  name: string
  scopes: Set<string>
  everyAgent: boolean
  agents: Set<string>
  days: number
  unasked: boolean
}

const EMPTY_FORM: KeyForm = {
  name: '', scopes: new Set(['chat']), everyAgent: true, agents: new Set(), days: 30, unasked: false,
}

function expired(at: string | null): boolean {
  return !!at && new Date(at).getTime() <= Date.now()
}

/** What a revoked key made: it stays, and the person decides what to do. */
export function RevokedKeyDialog({ result, onClose }: { result: KeyRevoked; onClose: () => void }) {
  return (
    <Dialog title="The key is revoked" onClose={onClose}>
      <div className="p-4">
        <p className="text-sm text-secondary mb-3">
          It no longer works. It made {result.created_total === 1 ? 'one thing, which stays' : `${result.created_total} things, and they stay`}.
        </p>
        <ul className="max-h-64 overflow-y-auto space-y-1 text-sm text-primary" data-testid="key-created-list">
          {result.created.map((c) => (
            <li key={`${c.kind}-${c.ref}`}>
              <Badge>{c.kind}</Badge> <span className="break-all">{c.label || c.ref}</span>
              {c.agent && <span className="text-tertiary"> on {c.agent}</span>}
            </li>
          ))}
        </ul>
        {result.created_total > result.created.length && (
          <p className="text-xs text-tertiary mt-2">The newest {result.created.length} are listed.</p>
        )}
        <div className="flex justify-end mt-3"><Button onClick={onClose}>Done</Button></div>
      </div>
    </Dialog>
  )
}

export function ApiKeysSection() {
  const dialogs = useDialogs()
  const sub = useAuth()?.user?.sub ?? ''
  const { data: keys = [], isLoading } = useUserApiKeys()
  const { data: agents = [] } = useAgents()
  const createM = useCreateUserApiKey()
  const revokeM = useRevokeUserApiKey()
  const [form, setForm] = useState<KeyForm | null>(null)
  const [createdKey, setCreatedKey] = useState<CreatedUserApiKey | null>(null)
  const [createError, setCreateError] = useState('')
  const [asking, setAsking] = useState<{ method: string; provider: string } | null>(null)
  const [password, setPassword] = useState('')
  const [revoked, setRevoked] = useState<KeyRevoked | null>(null)

  const closeForm = () => { setForm(null); setAsking(null); setPassword(''); setCreateError('') }

  /** One try at the mint: the failure, or null when the key was made. */
  const attempt = async (f: KeyForm, proof: { password?: string; confirm_token?: string }): Promise<unknown> => {
    try {
      const created = await createM.mutateAsync({
        name: f.name.trim(),
        permissions: [...f.scopes],
        agents: f.everyAgent ? [] : [...f.agents],
        expires_in_days: f.days,
        run_without_asking: f.unasked,
        ...proof,
      })
      setCreatedKey(created)
      closeForm()
      return null
    } catch (err) {
      return err
    }
  }

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!form) return
    setCreateError('')
    if (!form.name.trim()) return setCreateError('Give the key a name.')
    if (form.scopes.size === 0) return setCreateError('Pick at least one permission.')
    if (!form.everyAgent && form.agents.size === 0) return setCreateError('Pick at least one agent.')
    const armed = sub ? takeConfirm(sub) : null
    const failed = await attempt(form, asking?.method === KEY_CONFIRM.password ? { password } : armed ? { confirm_token: armed } : {})
    if (!failed) return
    if (!(failed instanceof KeyConfirmRequired)) return setCreateError(errorText(failed) || 'The key was not created.')
    if (failed.method !== KEY_CONFIRM.passkey) return setAsking({ method: failed.method, provider: failed.provider })
    let again: unknown
    try {
      again = await attempt(form, { confirm_token: await passkeyConfirm() })
    } catch (err) {
      again = err
    }
    if (again) setCreateError(errorText(again) || 'The key was not created.')
  }

  const onRevoke = async (id: string, name: string) => {
    if (!(await dialogs.confirm({
      title: `Revoke the key "${name}"?`,
      body: 'Whatever uses it fails at once.',
      confirmLabel: 'Revoke',
      tone: 'danger',
    }))) return
    const result = await revokeM.mutateAsync(id).catch(toastError('The key was not revoked'))
    if (result && result.created_total > 0) setRevoked(result)
  }

  const toggle = (set: Set<string>, id: string, on: boolean) => {
    const next = new Set(set)
    if (on) next.add(id)
    else next.delete(id)
    return next
  }

  return (
    <div className="mb-8">
      <h2 className="font-display text-lg font-semibold text-primary mb-3">API Keys</h2>
      <p className="text-sm text-secondary mb-2">
        A key lets a program use OtoDock as you. It never does more than you can, only what you tick here.
      </p>
      <p className="text-sm text-secondary mb-4">
        Use a key from a script, or with the <a className="underline" href="/v1/api/otodock.py" download="otodock.py">otodock command</a>.
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
                  {k.run_without_asking && <Badge variant="warning">Runs without asking</Badge>}
                  {k.revoked_at && <Badge variant="danger">revoked</Badge>}
                  {!k.revoked_at && expired(k.expires_at) && <Badge variant="danger">expired</Badge>}
                </div>
                <p className="text-xs text-secondary mt-0.5">
                  {k.agents.length ? k.agents.join(', ') : 'All your agents'}
                  {' · '}
                  {k.expires_at ? `ends ${new Date(k.expires_at).toLocaleDateString()}` : 'made before keys had an end date'}
                  {k.last_used_at ? ` · last used ${new Date(k.last_used_at).toLocaleString()}` : ' · never used'}
                </p>
              </div>
              {!k.revoked_at && (
                <button onClick={() => onRevoke(k.id, k.name)} className={BUTTON_DANGER_QUIET}>
                  Revoke
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <Button variant="primary" onClick={() => { setCreateError(''); setForm({ ...EMPTY_FORM, scopes: new Set(EMPTY_FORM.scopes), agents: new Set() }) }} className="mt-3">
        + Create key
      </Button>

      {form && (
        <Dialog title="Create API key" onClose={closeForm} busy={createM.isPending}>
          <form onSubmit={onCreate} className="space-y-3 p-4">
            <div>
              <Label htmlFor="user-key-name">Name</Label>
              <Input
                id="user-key-name"
                autoFocus
                value={form.name}
                onChange={(e) => { setForm({ ...form, name: e.target.value }); setCreateError('') }}
                placeholder="e.g. Nightly report script"
              />
            </div>
            <fieldset>
              <div className="mb-1">
                <legend className="inline text-xs font-medium text-primary">What it may do</legend>
                <InfoTip label="What it may do">
                  A key with chat reaches what your chats reach on its agents. That includes the agent's tools and the agents it delegates to.
                </InfoTip>
              </div>
              {SCOPES.map((s) => {
                const locked = s.id === TASKS && !form.unasked
                return (
                  <div key={s.id}>
                    <label className={`flex items-center gap-2 text-sm ${locked ? 'text-tertiary' : ''}`}>
                      <input
                        type="checkbox"
                        checked={form.scopes.has(s.id)}
                        disabled={locked}
                        onChange={(e) => setForm({ ...form, scopes: toggle(form.scopes, s.id, e.target.checked) })}
                        className="accent-action"
                      />
                      <span><code>{s.id}</code>: {s.label}</span>
                    </label>
                    {locked && (
                      <p className="ml-6 text-xs text-tertiary" data-testid="tasks-needs-unasked">
                        A task runs with nobody to ask. Tick "May run without asking" below to allow it.
                      </p>
                    )}
                  </div>
                )
              })}
            </fieldset>
            <fieldset>
              <legend className="text-xs font-medium text-primary mb-1">Which agents</legend>
              <label className="flex items-center gap-2 text-sm">
                <input type="radio" name="key-agents" className="accent-action" checked={form.everyAgent}
                  onChange={() => setForm({ ...form, everyAgent: true })} />
                All your agents
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="radio" name="key-agents" className="accent-action" checked={!form.everyAgent}
                  onChange={() => setForm({ ...form, everyAgent: false })} />
                Only these
              </label>
              {!form.everyAgent && (
                <div className="ml-6 mt-1 max-h-40 overflow-y-auto space-y-1">
                  {agents.map((a) => (
                    <label key={a.name} className="flex items-center gap-2 text-sm">
                      <input type="checkbox" className="accent-action" checked={form.agents.has(a.name)}
                        onChange={(e) => setForm({ ...form, agents: toggle(form.agents, a.name, e.target.checked) })} />
                      {a.display_name || a.name}
                    </label>
                  ))}
                </div>
              )}
            </fieldset>
            <div>
              <Label htmlFor="user-key-days">It ends after</Label>
              <select
                id="user-key-days"
                value={form.days}
                onChange={(e) => setForm({ ...form, days: Number(e.target.value) })}
                className="w-full rounded-control border border-strong bg-surface-card px-2 py-1.5 text-sm text-primary"
              >
                {EXPIRY_DAYS.map((d) => <option key={d} value={d}>{d} days</option>)}
              </select>
            </div>
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" className="accent-action mt-0.5" checked={form.unasked}
                onChange={(e) => setForm({
                  ...form,
                  unasked: e.target.checked,
                  scopes: e.target.checked ? form.scopes : toggle(form.scopes, TASKS, false),
                })} />
              <span>
                May run without asking
                <span className="block text-xs text-tertiary">
                  Nobody approves what this key starts. It can send into chats in Auto and Don't Ask, and run tasks. Leave this off unless you need it.
                </span>
                <span className="block text-xs text-tertiary mt-1" data-testid="ask-still-runs">
                  Off, the key sends only into chats in Ask or Plan. There you approve each step that changes something. Four things still run without asking. The agent reads. It controls a paired computer's browser or screen where its owner allowed that. It uses the tools that never ask, such as saving a memory. It repeats a tool you allowed earlier in the same turn.
                </span>
              </span>
            </label>
            {asking?.method === KEY_CONFIRM.password && (
              <div>
                <Label htmlFor="user-key-password">Confirm it is you</Label>
                <Input id="user-key-password" type="password" autoComplete="current-password" autoFocus
                  value={password} placeholder="Your password" onChange={(e) => setPassword(e.target.value)} />
              </div>
            )}
            {asking?.method === KEY_CONFIRM.oidc && (
              <div className="rounded-card bg-surface-sunken p-3 text-xs text-secondary">
                <p>This page goes to {asking.provider || 'your identity provider'} to check it is you, then comes back here. Make the key again when you are back.</p>
                <Button size="xs" type="button" className="mt-2"
                  onClick={() => startOidcConfirm(RETURN_TO).catch((err) => setCreateError(errorText(err)))}>
                  Continue to {asking.provider || 'your identity provider'}
                </Button>
              </div>
            )}
            {asking?.method === KEY_CONFIRM.none && (
              <p className="text-xs text-secondary">A key with these permissions needs you to confirm it is you. This account has no password or passkey to do that.</p>
            )}
            {createError && <p role="alert" className="text-xs text-status-danger-fg">{createError}</p>}
            <div className="flex justify-end gap-2 pt-2">
              <Button onClick={closeForm} disabled={createM.isPending}>Cancel</Button>
              <Button type="submit" variant="primary" disabled={createM.isPending || (asking?.method === KEY_CONFIRM.password && !password)}>
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
            <div className="bg-surface-sunken text-primary rounded-card p-2 mb-3 font-mono text-xs break-all" data-testid="created-api-key">{createdKey.key}</div>
            <div className="flex justify-end gap-2">
              <Button variant="primary" onClick={() => { void copyOrSay(createdKey.key) }}>Copy to clipboard</Button>
              <Button onClick={() => setCreatedKey(null)}>Done</Button>
            </div>
          </div>
        </Dialog>
      )}

      {revoked && <RevokedKeyDialog result={revoked} onClose={() => setRevoked(null)} />}
    </div>
  )
}
