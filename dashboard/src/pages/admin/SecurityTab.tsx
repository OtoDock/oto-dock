import { useState } from 'react'
import { apiFetch } from '../../api/auth'
import {
  useBearerAllowlist,
  useAddBearerAllowlist,
  useRemoveBearerAllowlist,
  useRestoreBearerAllowlist,
} from '../../api/oauth'
import { toastError } from '../../lib/http/apiJson'
import { usePlatformSettings, useSavePlatformSettings } from './PlatformPage.hooks'
import { SaveState, SettingRow } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'
import InfoTip from '../../components/ui/InfoTip'
import Switch from '../../components/ui/Switch'
import { FORWARDING_CONSEQUENCE, forwardingAdvice, forwardingSeen } from '../../components/admin/forwardingAdvice'
import type { ForwardingWarning } from '../../components/admin/forwardingAdvice'
import Button from '../../components/ui/Button'
import { Input, Select } from '../../components/ui/Field'
import { CARD } from '../../components/ui/classes'

// ---------------------------------------------------------------------------
// Security tab
// ---------------------------------------------------------------------------

// OAuth Bearer Allowlist — moved here from the removed Service Accounts tab.
// Controls which (provider, host) pairs may receive a user's OAuth token via
// HTTP Authorization: Bearer injection; "Restore defaults" re-adds the
// vendor-official hosts an admin may have deleted.
function BearerAllowlistSection() {
  const { data: entries, isLoading } = useBearerAllowlist()
  const addEntry = useAddBearerAllowlist()
  const removeEntry = useRemoveBearerAllowlist()
  const restoreDefaults = useRestoreBearerAllowlist()
  const [provider, setProvider] = useState('')
  const [host, setHost] = useState('')
  const [error, setError] = useState('')

  const handleAdd = async () => {
    setError('')
    if (!provider.trim() || !host.trim()) {
      setError('Enter a provider and a host.')
      return
    }
    try {
      await addEntry.mutateAsync({
        providerId: provider.trim(),
        hostPattern: host.trim(),
      })
      setProvider('')
      setHost('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'The entry was not added.')
    }
  }

  return (
    <div className={`${CARD} p-4`}>
      <div className="flex items-center justify-between mb-1">
        <h3 className="text-sm font-semibold text-primary">OAuth Bearer Allowlist</h3>
        <Button variant="secondary" size="xs"
          onClick={() => { void restoreDefaults.mutateAsync().catch(toastError('The defaults were not restored')) }}
          disabled={restoreDefaults.isPending}
        >
          {restoreDefaults.isPending ? 'Restoring…' : 'Restore defaults'}
        </Button>
      </div>
      <p className="text-xs text-tertiary mb-4">
        Controls which MCPs may receive a user's OAuth token. MCPs that use
        OAuth need it to sign in.
      </p>

      <div className="flex gap-2 mb-4 items-end">
        <div className="flex-1">
          <label className="block text-xs text-secondary mb-1">Provider ID</label>
          <Input
            type="text"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            placeholder="slack"
          />
        </div>
        <div className="flex-1">
          <label className="block text-xs text-secondary mb-1">Host pattern</label>
          <Input
            type="text"
            value={host}
            onChange={(e) => setHost(e.target.value)}
            placeholder="mcp.slack.com or *.slack.com"
          />
        </div>
        <Button variant="primary" size="md"
          onClick={handleAdd}
          disabled={addEntry.isPending}
        >
          {addEntry.isPending ? 'Adding…' : 'Add'}
        </Button>
      </div>
      {error && <div className="text-sm text-status-danger-fg mb-3">{error}</div>}

      {isLoading ? (
        <div className="text-sm text-tertiary">Loading…</div>
      ) : entries && entries.length > 0 ? (
        <div className="divide-y divide-subtle">
          {entries.map((e) => (
            <div
              key={e.id}
              className="flex items-center justify-between py-2 text-sm"
            >
              <div className="font-mono text-primary">
                <span className="font-medium">{e.provider_id}</span>
                <span className="mx-2 text-tertiary">→</span>
                <span>{e.host_pattern}</span>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-xs text-tertiary">{e.added_by}</span>
                <button
                  onClick={() => { void removeEntry.mutateAsync(e.id).catch(toastError('The entry was not removed')) }}
                  disabled={removeEntry.isPending}
                  className="text-sm text-status-danger-fg hover:underline"
                >
                  Remove
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-sm text-tertiary">No entries.</div>
      )}
    </div>
  )
}


export default function SecurityTab() {
  const { data, isLoading } = usePlatformSettings()
  const saveMutation = useSavePlatformSettings()
  const { stateOf, run } = useSaveState()

  // SMTP
  const [smtpHost, setSmtpHost] = useState('')
  const [smtpPort, setSmtpPort] = useState('587')
  const [smtpUser, setSmtpUser] = useState('')
  const [smtpPassword, setSmtpPassword] = useState('')
  const [smtpFrom, setSmtpFrom] = useState('')
  const [smtpTls, setSmtpTls] = useState(true)

  // Cloudflare Turnstile
  const [turnstileSiteKey, setTurnstileSiteKey] = useState('')
  const [turnstileSecretKey, setTurnstileSecretKey] = useState('')


  // Password policy
  const [pwMinScore, setPwMinScore] = useState('3')
  const [pwMinLength, setPwMinLength] = useState('8')

  // SMTP Test
  const [testEmail, setTestEmail] = useState('')
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null)
  const [testing, setTesting] = useState(false)

  // The saved settings fill the form when they arrive or change (in render).
  const [filledFrom, setFilledFrom] = useState(data)
  if (data && filledFrom !== data) {
    setFilledFrom(data)
    {
      setSmtpHost(data.smtp_host || '')
      setSmtpPort(data.smtp_port || '587')
      setSmtpUser(data.smtp_user || '')
      setSmtpFrom(data.smtp_from || '')
      setSmtpTls(data.smtp_tls !== 'false')
      setTurnstileSiteKey(data.turnstile_site_key || '')
      setPwMinScore(data.password_min_score || '3')
      setPwMinLength(data.password_min_length || '8')
    }
  }

  // A refusal toasts the server's reason (turning the second-factor
  // requirement on is refused while the admin has none).
  const save = (field: string, value: string | boolean, revert?: () => void) => {
    void run(field, () => saveMutation.mutateAsync({ [field]: value }),
      { failTitle: 'The security setting was not saved', revert })
  }

  const saveSmtp = () => {
    void run('smtp', () => saveMutation.mutateAsync({
      smtp_host: smtpHost, smtp_port: smtpPort, smtp_user: smtpUser,
      smtp_password: smtpPassword || undefined,
      smtp_from: smtpFrom, smtp_tls: smtpTls ? 'true' : 'false',
    }).then(() => setSmtpPassword('')), { failTitle: 'The email settings were not saved' })
  }

  const testSmtp = async () => {
    setTesting(true); setTestResult(null)
    try {
      const res = await apiFetch('/v1/admin/smtp/test', {
        method: 'POST',
        body: JSON.stringify({
          host: smtpHost, port: parseInt(smtpPort) || 587, user: smtpUser,
          password: smtpPassword, from_addr: smtpFrom || smtpUser,
          tls: smtpTls, test_email: testEmail,
        }),
      })
      const d = await res.json()
      setTestResult(d)
    } catch { setTestResult({ success: false, message: 'The test did not run. Try again.' }) }
    finally { setTesting(false) }
  }

  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>

  const warnings: ForwardingWarning[] = data?.forwarding_warnings || []

  return (
    <div className="space-y-6">
      {warnings.length > 0 && (
        <div role="alert" className="rounded-card border border-status-warning-border bg-status-warning-bg p-4 text-sm text-status-warning-fg">
          <div className="font-semibold">Client addresses are not being resolved</div>
          <ul className="mt-1 list-disc pl-5 space-y-0.5">
            {warnings.map((w) => (
              <li key={`${w.case}:${w.peer}`}>
                {forwardingAdvice(w, !!data?.cloud)}
                {forwardingSeen(w) && ` ${forwardingSeen(w)}`}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-xs">{FORWARDING_CONSEQUENCE}</p>
        </div>
      )}
      {/* On the OtoDock cloud these are operator-managed (values come from
          config.env via OTODOCK_FORCED_SETTINGS; writes are rejected
          server-side) — hide them from the customer-admin. */}
      {!data?.cloud && <>
      {/* Two-Factor Authentication policy */}
      <div className={`${CARD} p-4`}>
        <div className="flex items-center justify-between gap-3">
          <div>
            <div>
              <h3 className="inline text-sm font-semibold text-primary">Require Two-Factor Authentication</h3>
              <InfoTip label="Require Two-Factor Authentication">
                Accounts with a local password must set up a second factor at their next sign-in.
                It can be an authenticator app or a passkey.
              </InfoTip>
              <SaveState state={stateOf('require_2fa')} />
            </div>
            <p className="text-xs text-tertiary mt-1">
              SSO accounts are exempt, because their
              identity provider handles MFA.
            </p>
          </div>
          <Switch
            aria-label="Require Two-Factor Authentication"
            checked={!!data?.require_2fa}
            disabled={(data?.forced_keys || []).includes('require_2fa')}
            title={(data?.forced_keys || []).includes('require_2fa') ? 'Managed by the operator' : undefined}
            onChange={(on) => save('require_2fa', on)}
          />
        </div>
        <div className="mt-4 pt-4 border-t border-subtle grid grid-cols-1 sm:grid-cols-2 gap-4 items-start">
          <div>
            <div className="mb-1">
              <label className="text-xs text-secondary">Passkey Sign-In</label>
              <InfoTip label="Passkey Sign-In">
                A passkey always asks for a fingerprint, face or PIN on the device. So
                passwordless sign-in is already multi-factor and resists phishing.
              </InfoTip>
              <SaveState state={stateOf('passkey_login_mode')} />
            </div>
            <Select value={data?.passkey_login_mode || 'passwordless'}
              disabled={(data?.forced_keys || []).includes('passkey_login_mode')}
              onChange={e => save('passkey_login_mode', e.target.value)}>
              <option value="passwordless">Passwordless: a passkey signs in on its own</option>
              <option value="second_factor">Second factor only: a passkey after the password</option>
            </Select>
            <p className="text-2xs text-tertiary mt-1">
              Passkeys work only with a public dashboard URL that starts with https.
            </p>
          </div>
        </div>
      </div>

      {/* Password Policy */}
      <div className={`${CARD} p-4`}>
        <h3 className="text-sm font-semibold text-primary mb-1">Password Policy</h3>
        <p className="text-xs text-tertiary mb-4">Configure password strength requirements for local accounts.</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <p className="text-xs text-secondary mb-1"><label>Minimum Strength</label><SaveState state={stateOf('password_min_score')} /></p>
            <Select value={pwMinScore} onChange={e => setPwMinScore(e.target.value)}
              onBlur={() => save('password_min_score', pwMinScore, () => setPwMinScore(data?.password_min_score || '3'))}>
              <option value="0">Very weak (0): no restriction</option>
              <option value="1">Weak (1): basic</option>
              <option value="2">Fair (2): moderate</option>
              <option value="3">Strong (3): recommended</option>
              <option value="4">Very strong (4): strictest</option>
            </Select>
          </div>
          <div>
            <p className="text-xs text-secondary mb-1"><label>Minimum Length</label><SaveState state={stateOf('password_min_length')} /></p>
            <Input type="number" value={pwMinLength} min={4} max={128}
              onChange={e => setPwMinLength(e.target.value)}
              onBlur={() => save('password_min_length', pwMinLength)}
              className="text-right" />
          </div>
        </div>
      </div>

      {/* SMTP */}
      <div className={`${CARD} p-4`}>
        <div className="flex items-center gap-2 mb-1">
          <h3 className="text-sm font-semibold text-primary">SMTP Email</h3>
          <SaveState state={stateOf('smtp')} besideButton />
        </div>
        <p className="text-xs text-tertiary mb-4">Used for password resets and user invites.</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
          <div>
            <label className="block text-xs text-secondary mb-1">SMTP Host</label>
            <Input type="text" value={smtpHost} onChange={e => setSmtpHost(e.target.value)}
              placeholder="mail.example.com" />
          </div>
          <div>
            <label className="block text-xs text-secondary mb-1">Port</label>
            <Input type="text" value={smtpPort} onChange={e => setSmtpPort(e.target.value)} />
          </div>
          <div>
            <label className="block text-xs text-secondary mb-1">Username</label>
            <Input type="text" value={smtpUser} onChange={e => setSmtpUser(e.target.value)}
              placeholder="noreply@example.com" />
          </div>
          <div>
            <label className="block text-xs text-secondary mb-1">Password {data?.smtp_password_set && <span className="text-status-success-fg">Set</span>}</label>
            <Input type="password" value={smtpPassword} onChange={e => setSmtpPassword(e.target.value)}
              placeholder={data?.smtp_password_set ? '••••••••' : 'SMTP password'} />
          </div>
          <div>
            <label className="block text-xs text-secondary mb-1">From Address</label>
            <Input type="text" value={smtpFrom} onChange={e => setSmtpFrom(e.target.value)}
              placeholder="noreply@example.com" />
          </div>
          <div className="flex items-center gap-2 self-end py-2">
            <span id="smtp-tls-label" className="text-xs text-secondary">TLS</span>
            <Switch aria-labelledby="smtp-tls-label" checked={smtpTls} onChange={setSmtpTls} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" size="xs" onClick={saveSmtp} disabled={saveMutation.isPending}>
            {saveMutation.isPending ? 'Saving...' : 'Save SMTP'}
          </Button>
          <Input compact type="email" value={testEmail} onChange={e => setTestEmail(e.target.value)}
            placeholder="test@example.com"
            className="max-w-48" />
          <Button variant="secondary" size="xs" onClick={testSmtp} disabled={testing || !smtpHost}>
            {testing ? 'Testing...' : 'Test Connection'}
          </Button>
          {testResult && (
            <span className={`text-xs font-medium ${testResult.success ? 'text-status-success-fg' : 'text-status-danger-fg'}`}>
              {testResult.message}
            </span>
          )}
        </div>
      </div>

      </>}

      {/* Sharing (SHARING.md): the platform-wide switches for shares to
          agents and departments and for links. */}
      <div className={`${CARD} p-4 space-y-4`}>
        <div>
          <h3 className="text-sm font-semibold text-primary">Sharing</h3>
          <p className="text-xs text-tertiary mt-1">
            People share apps with colleagues by default. These switches cover sharing to whole
            agents and departments. They also cover links for people outside the platform. Every
            share and every link is listed in Admin, Shares.
          </p>
        </div>
        {([
          ['sharing_to_agents_enabled', 'Sharing to agents', 'An agent\'s editor or manager may place another agent\'s app in its Apps panel. Every member sees it. Off refuses new ones.', null],
          ['sharing_to_departments_enabled', 'Sharing to departments', 'An admin may place an app in every agent of a department. The app follows the department as it changes. Off refuses new ones.', null],
          ['sharing_external_enabled', 'External links', 'Members may make links that open an app for someone without an account. Links need a password by default.', null],
          ['sharing_public_links_enabled', 'Links without a password', 'A link may be made public. Off keeps every link behind a password.', 'Anyone with a public link can open it.'],
          ['user_directory_visible_to_members', 'User directory when sharing', 'Members pick colleagues and agents from a list. Off means they type an exact username or email. They see only their own agents. The platform never says whether a name matched.', null],
        ] as const).map(([key, label, help, hint]) => (
          <SettingRow key={key} label={label} description={hint} info={help} save={stateOf(key)}>
            <Switch aria-label={label} checked={!!data?.[key]} onChange={(on) => save(key, on)} />
          </SettingRow>
        ))}
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <span className="text-sm text-primary">Longest share expiry</span>
              <SaveState state={stateOf('sharing_max_expiry_days')} />
            </div>
            <p className="text-xs text-tertiary mt-0.5">In days: caps every share and link. A share left on “Never expires” gets this length. Empty lets a member choose “never”.</p>
          </div>
          <Input type="text" inputMode="numeric" defaultValue={data?.sharing_max_expiry_days || ''}
            key={data?.sharing_max_expiry_days || 'unset'}
            aria-label="Longest share expiry in days" placeholder="No cap"
            onBlur={(e) => { if (e.target.value !== (data?.sharing_max_expiry_days || '')) save('sharing_max_expiry_days', e.target.value.trim()) }}
            className="max-w-24" />
        </div>
      </div>

      {/* Login Security (Cloudflare Turnstile). Sits OUTSIDE the cloud-managed guard:
          on the OtoDock cloud the keys are operator-managed (OTODOCK_TURNSTILE_* env)
          and this card shows only a read-only "managed" badge — never the keys.
          Self-hosters configure their own keys (secret write-only). */}
      {(data?.turnstile_managed || !data?.cloud) && (
        <div className={`${CARD} p-4`}>
          <h3 className="text-sm font-semibold text-primary mb-1">Login Security with Cloudflare Turnstile</h3>
          {data?.turnstile_managed ? (
            <p className="text-xs text-secondary">
              Bot protection managed by OtoDock: <span className="text-status-success-fg font-medium">Configured</span>.
            </p>
          ) : (
            <>
              <p className="text-xs text-tertiary mb-4">Optional. Protects the login page from automated attacks.</p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <p className="text-xs text-secondary mb-1"><label>Site Key</label><SaveState state={stateOf('turnstile_site_key')} /></p>
                  <Input type="text" value={turnstileSiteKey} onChange={e => setTurnstileSiteKey(e.target.value)}
                    onBlur={() => save('turnstile_site_key', turnstileSiteKey)}
                    placeholder="0x4AAA..." />
                </div>
                <div>
                  <p className="text-xs text-secondary mb-1">
                    <label>Secret Key {data?.turnstile_secret_key_set && <span className="text-status-success-fg">Set</span>}</label>
                    <SaveState state={stateOf('turnstile_secret_key')} />
                  </p>
                  <Input type="password" value={turnstileSecretKey} onChange={e => setTurnstileSecretKey(e.target.value)}
                    onBlur={() => { if (turnstileSecretKey) save('turnstile_secret_key', turnstileSecretKey) }}
                    placeholder={data?.turnstile_secret_key_set ? '••••••••' : 'Secret key'} />
                </div>
              </div>
            </>
          )}
        </div>
      )}

      {/* OAuth Bearer Allowlist — gates which MCPs may receive a user's OAuth
          token. Sits OUTSIDE the cloud-managed guard above: it's a security
          control that matters on both self-hosted and cloud installs. */}
      <BearerAllowlistSection />

      {/* Network restriction is now per-user only (Users page → local-only
          toggle). The platform-wide LAN knob was removed. */}

    </div>
  )
}
