import { useState } from 'react'
import {
  KEY_KIND,
  useAdminApiKeys,
  useAdminRevokeApiKey,
  useSetApiKeysEnabled,
  type AdminApiKey,
  type KeyRevoked,
} from '../../api/userApiKeys'
import InfoTip from '../../components/ui/InfoTip'
import { Badge } from '../../components/ui/SettingsControls'
import Switch from '../../components/ui/Switch'
import { BUTTON_DANGER_QUIET, CARD } from '../../components/ui/classes'
import { useDialogs } from '../../components/ui/dialogs'
import { toastError } from '../../lib/http/apiJson'
import { RevokedKeyDialog } from '../UserSettings.apiKeys'

function whose(k: AdminApiKey): string {
  return k.kind === KEY_KIND.agent ? `Agent ${k.agent}` : k.owner || k.owner_email || 'Someone who left'
}

/** Every API key of the install, its revocation, and the switch for all of them. */
export default function ApiKeysCard() {
  const dialogs = useDialogs()
  const { data, isLoading, error } = useAdminApiKeys()
  const setEnabled = useSetApiKeysEnabled()
  const revoke = useAdminRevokeApiKey()
  const [revoked, setRevoked] = useState<KeyRevoked | null>(null)

  const onRevoke = async (k: AdminApiKey) => {
    if (!(await dialogs.confirm({
      title: `Revoke the key "${k.name}"?`,
      body: `It belongs to ${whose(k)}. Whatever uses it fails at once.`,
      confirmLabel: 'Revoke',
      tone: 'danger',
    }))) return
    const result = await revoke.mutateAsync({ kind: k.kind, id: k.id }).catch(toastError('The key was not revoked'))
    if (result && result.created_total > 0) setRevoked(result)
  }

  return (
    <div className={`${CARD} p-4`} data-testid="admin-api-keys">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="inline text-sm font-semibold text-primary">API Keys</h3>
          <InfoTip label="API Keys">
            People make their own keys in their settings, and such a key acts as its person. An agent's managers make its webhook keys. Turning this off stops every key at once, webhook senders included.
          </InfoTip>
          <p className="text-xs text-tertiary mt-1">Every key on this platform. You can revoke any of them.</p>
        </div>
        {/* Shown once the server said which way it is: never a guess. */}
        {data && (
          <Switch
            aria-label="Allow API keys"
            checked={data.enabled}
            disabled={setEnabled.isPending}
            onChange={(on) => { setEnabled.mutateAsync(on).catch(toastError('The setting was not saved')) }}
          />
        )}
      </div>
      {error ? <p className="mt-3 text-xs text-status-danger-fg">{(error as Error).message}</p> : null}
      {!isLoading && !error && (data?.keys.length ?? 0) === 0 && (
        <p className="mt-3 text-xs text-secondary">No API keys yet.</p>
      )}
      {(data?.keys.length ?? 0) > 0 && (
        <ul className="mt-3 divide-y divide-subtle">
          {data?.keys.map((k) => (
            <li key={`${k.kind}-${k.id}`} className="flex items-center gap-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap text-sm">
                  <span className="font-medium text-primary">{k.name}</span>
                  <span className="text-secondary">{whose(k)}</span>
                  {k.permissions.map((p) => <Badge key={p}>{p}</Badge>)}
                  {k.run_without_asking && <Badge variant="warning">Runs without asking</Badge>}
                </div>
                <p className="text-xs text-tertiary mt-0.5">
                  {k.kind === KEY_KIND.user && (k.agents?.length ? k.agents.join(', ') : 'All their agents')}
                  {k.kind === KEY_KIND.user && ' · '}
                  {k.kind === KEY_KIND.user && (k.expires_at ? `ends ${new Date(k.expires_at).toLocaleDateString()}` : 'no end date')}
                  {k.kind === KEY_KIND.user && ' · '}
                  {k.last_used_at ? `last used ${new Date(k.last_used_at).toLocaleString()}` : 'never used'}
                </p>
              </div>
              <button onClick={() => onRevoke(k)} className={BUTTON_DANGER_QUIET}>Revoke</button>
            </li>
          ))}
        </ul>
      )}
      {revoked && <RevokedKeyDialog result={revoked} onClose={() => setRevoked(null)} />}
    </div>
  )
}
