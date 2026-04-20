import { useState, useEffect } from 'react'
import { apiFetch } from '../../api/auth'
import {
  useBearerAllowlist,
  useAddBearerAllowlist,
  useRemoveBearerAllowlist,
  useRestoreBearerAllowlist,
} from '../../api/oauth'
import { usePlatformSettings, useSavePlatformSettings } from './PlatformPage.hooks'
import { SavedBadge } from './PlatformPage.shared'

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
      setError('Provider and host required')
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
      setError(e instanceof Error ? e.message : 'Failed to add')
    }
  }

  return (
    <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-4">
      <div className="flex items-center justify-between mb-1">
        <h3 className="text-sm font-semibold text-p-text">OAuth Bearer Allowlist</h3>
        <button
          onClick={() => restoreDefaults.mutate()}
          disabled={restoreDefaults.isPending}
          className="text-xs px-3 py-1.5 border border-p-border-light rounded-lg text-p-text-secondary hover:bg-p-bg disabled:opacity-50"
        >
          {restoreDefaults.isPending ? 'Restoring…' : 'Restore defaults'}
        </button>
      </div>
      <p className="text-xs text-p-text-light mb-4">
        Controls which MCPs may receive a user's OAuth token, so OAuth-based
        MCPs can authenticate.
      </p>

      <div className="flex gap-2 mb-4 items-end">
        <div className="flex-1">
          <label className="block text-xs text-p-text-secondary mb-1">Provider ID</label>
          <input
            type="text"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            placeholder="slack"
            className="w-full px-3 py-2 border border-p-border-light rounded-lg text-sm bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
          />
        </div>
        <div className="flex-1">
          <label className="block text-xs text-p-text-secondary mb-1">Host pattern</label>
          <input
            type="text"
            value={host}
            onChange={(e) => setHost(e.target.value)}
            placeholder="mcp.slack.com or *.slack.com"
            className="w-full px-3 py-2 border border-p-border-light rounded-lg text-sm bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
          />
        </div>
        <button
          onClick={handleAdd}
          disabled={addEntry.isPending}
          className="px-4 py-2 bg-brand hover:bg-brand-hover text-white rounded-lg text-sm disabled:opacity-50"
        >
          {addEntry.isPending ? 'Adding…' : 'Add'}
        </button>
      </div>
      {error && <div className="text-sm text-p-accent-red mb-3">{error}</div>}

      {isLoading ? (
        <div className="text-sm text-p-text-light">Loading…</div>
      ) : entries && entries.length > 0 ? (
        <div className="space-y-1">
          {entries.map((e) => (
            <div
              key={e.id}
              className="flex items-center justify-between px-3 py-2 border border-p-border-light rounded-lg text-sm"
            >
              <div className="font-mono text-p-text">
                <span className="font-medium">{e.provider_id}</span>
                <span className="mx-2 text-p-text-light">→</span>
                <span>{e.host_pattern}</span>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-xs text-p-text-light">{e.added_by}</span>
                <button
                  onClick={() => removeEntry.mutate(e.id)}
                  disabled={removeEntry.isPending}
                  className="text-sm text-p-accent-red hover:underline"
                >
                  Remove
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-sm text-p-text-light">No entries.</div>
      )}
    </div>
  )
}


export default function SecurityTab() {
  const { data, isLoading } = usePlatformSettings()
  const saveMutation = useSavePlatformSettings()
  const [savedField, setSavedField] = useState('')

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

  useEffect(() => {
    if (data) {
      setSmtpHost(data.smtp_host || '')
      setSmtpPort(data.smtp_port || '587')
      setSmtpUser(data.smtp_user || '')
      setSmtpFrom(data.smtp_from || '')
      setSmtpTls(data.smtp_tls !== 'false')
      setTurnstileSiteKey(data.turnstile_site_key || '')
      setPwMinScore(data.password_min_score || '3')
      setPwMinLength(data.password_min_length || '8')
    }
  }, [data])

  const save = (field: string, value: string | boolean) => {
    saveMutation.mutate(
      { [field]: value },
      { onSuccess: () => { setSavedField(field); setTimeout(() => setSavedField(''), 2000) } }
    )
  }

  const saveSmtp = () => {
    saveMutation.mutate({
      smtp_host: smtpHost, smtp_port: smtpPort, smtp_user: smtpUser,
      smtp_password: smtpPassword || undefined as any,
      smtp_from: smtpFrom, smtp_tls: smtpTls ? 'true' : 'false',
    }, { onSuccess: () => { setSavedField('smtp'); setTimeout(() => setSavedField(''), 2000); setSmtpPassword('') } })
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
    } catch { setTestResult({ success: false, message: 'Request failed' }) }
    finally { setTesting(false) }
  }

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>

  return (
    <div className="space-y-6">
      {/* On the OtoDock cloud these are operator-managed (values come from
          config.env via OTODOCK_FORCED_SETTINGS; writes are rejected
          server-side) — hide them from the customer-admin. */}
      {!data?.cloud && <>
      {/* Two-Factor Authentication policy */}
      <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-p-text">Require Two-Factor Authentication</h3>
              <SavedBadge show={savedField === 'require_2fa'} />
            </div>
            <p className="text-xs text-p-text-light mt-1">
              Local-password accounts must set up a second factor (authenticator app or passkey)
              at their next sign-in. SSO accounts are exempt — their identity provider owns MFA.
            </p>
          </div>
          <button type="button" role="switch" aria-checked={!!data?.require_2fa}
            disabled={(data?.forced_keys || []).includes('require_2fa')}
            title={(data?.forced_keys || []).includes('require_2fa') ? 'Managed by the operator' : undefined}
            onClick={() => save('require_2fa', !data?.require_2fa)}
            className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors disabled:opacity-50 ${data?.require_2fa ? 'bg-brand' : 'bg-gray-300 dark:bg-gray-600'}`}>
            <span className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-sm transition ${data?.require_2fa ? 'translate-x-4' : 'translate-x-0'}`} />
          </button>
        </div>
        <div className="mt-4 pt-4 border-t border-p-border-light grid grid-cols-1 sm:grid-cols-2 gap-4 items-start">
          <div>
            <label className="block text-xs text-p-text-secondary mb-1">Passkey Sign-In</label>
            <select value={data?.passkey_login_mode || 'passwordless'}
              disabled={(data?.forced_keys || []).includes('passkey_login_mode')}
              onChange={e => save('passkey_login_mode', e.target.value)}
              className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 disabled:opacity-50">
              <option value="passwordless">Passwordless — passkey is a primary sign-in method</option>
              <option value="second_factor">Second factor only — passkeys after a correct password</option>
            </select>
            <SavedBadge show={savedField === 'passkey_login_mode'} />
          </div>
          <p className="text-[11px] text-p-text-light sm:pt-5">
            Passkey sign-in always requires on-device verification (biometric or PIN), so
            passwordless is itself multi-factor and phishing-resistant. Applies only when
            passkeys are enabled (HTTPS public dashboard URL).
          </p>
        </div>
      </div>

      {/* Password Policy */}
      <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-4">
        <h3 className="text-sm font-semibold text-p-text mb-1">Password Policy</h3>
        <p className="text-xs text-p-text-light mb-4">Configure password strength requirements for local accounts.</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs text-p-text-secondary mb-1">Minimum Strength</label>
            <select value={pwMinScore} onChange={e => setPwMinScore(e.target.value)}
              onBlur={() => save('password_min_score', pwMinScore)}
              className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30">
              <option value="0">Very weak (0) — no restriction</option>
              <option value="1">Weak (1) — basic</option>
              <option value="2">Fair (2) — moderate</option>
              <option value="3">Strong (3) — recommended</option>
              <option value="4">Very strong (4) — strictest</option>
            </select>
            <SavedBadge show={savedField === 'password_min_score'} />
          </div>
          <div>
            <label className="block text-xs text-p-text-secondary mb-1">Minimum Length</label>
            <input type="number" value={pwMinLength} min={4} max={128}
              onChange={e => setPwMinLength(e.target.value)}
              onBlur={() => save('password_min_length', pwMinLength)}
              className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text text-right focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
            <SavedBadge show={savedField === 'password_min_length'} />
          </div>
        </div>
      </div>

      {/* SMTP */}
      <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-4">
        <div className="flex items-center gap-2 mb-1">
          <h3 className="text-sm font-semibold text-p-text">Email (SMTP)</h3>
          <SavedBadge show={savedField === 'smtp'} />
        </div>
        <p className="text-xs text-p-text-light mb-4">Used for password resets and user invites.</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
          <div>
            <label className="block text-xs text-p-text-secondary mb-1">SMTP Host</label>
            <input type="text" value={smtpHost} onChange={e => setSmtpHost(e.target.value)}
              placeholder="mail.example.com"
              className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
          </div>
          <div>
            <label className="block text-xs text-p-text-secondary mb-1">Port</label>
            <input type="text" value={smtpPort} onChange={e => setSmtpPort(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
          </div>
          <div>
            <label className="block text-xs text-p-text-secondary mb-1">Username</label>
            <input type="text" value={smtpUser} onChange={e => setSmtpUser(e.target.value)}
              placeholder="noreply@example.com"
              className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
          </div>
          <div>
            <label className="block text-xs text-p-text-secondary mb-1">Password {data?.smtp_password_set && <span className="text-green-600">(set)</span>}</label>
            <input type="password" value={smtpPassword} onChange={e => setSmtpPassword(e.target.value)}
              placeholder={data?.smtp_password_set ? '••••••••' : 'SMTP password'}
              className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
          </div>
          <div>
            <label className="block text-xs text-p-text-secondary mb-1">From Address</label>
            <input type="text" value={smtpFrom} onChange={e => setSmtpFrom(e.target.value)}
              placeholder="noreply@example.com"
              className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
          </div>
          <div className="flex items-center gap-2 self-end py-2">
            <label className="text-xs text-p-text-secondary">TLS</label>
            <button type="button" role="switch" aria-checked={smtpTls}
              onClick={() => setSmtpTls(!smtpTls)}
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${smtpTls ? 'bg-brand' : 'bg-gray-300 dark:bg-gray-600'}`}>
              <span className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-sm transition ${smtpTls ? 'translate-x-4' : 'translate-x-0'}`} />
            </button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button onClick={saveSmtp} disabled={saveMutation.isPending}
            className="px-3 py-1.5 text-xs font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50">
            {saveMutation.isPending ? 'Saving...' : 'Save SMTP'}
          </button>
          <input type="email" value={testEmail} onChange={e => setTestEmail(e.target.value)}
            placeholder="test@example.com"
            className="px-3 py-1.5 text-xs border border-p-border-light rounded-lg bg-p-bg text-p-text w-48" />
          <button onClick={testSmtp} disabled={testing || !smtpHost}
            className="px-3 py-1.5 text-xs font-medium border border-p-border-light rounded-lg text-p-text-secondary hover:bg-p-surface-hover disabled:opacity-50">
            {testing ? 'Testing...' : 'Test Connection'}
          </button>
          {testResult && (
            <span className={`text-xs font-medium ${testResult.success ? 'text-green-600 dark:text-green-400' : 'text-p-accent-red'}`}>
              {testResult.message}
            </span>
          )}
        </div>
      </div>

      </>}

      {/* Login Security (Cloudflare Turnstile). Sits OUTSIDE the cloud-managed guard:
          on the OtoDock cloud the keys are operator-managed (OTODOCK_TURNSTILE_* env)
          and this card shows only a read-only "managed" badge — never the keys.
          Self-hosters configure their own keys (secret write-only). */}
      {(data?.turnstile_managed || !data?.cloud) && (
        <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-4">
          <h3 className="text-sm font-semibold text-p-text mb-1">Login Security (Cloudflare Turnstile)</h3>
          {data?.turnstile_managed ? (
            <p className="text-xs text-p-text-secondary">
              Bot protection managed by OtoDock — <span className="text-green-600 dark:text-green-400 font-medium">Configured</span>.
            </p>
          ) : (
            <>
              <p className="text-xs text-p-text-light mb-4">Optional. Protects the login page from automated attacks.</p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-p-text-secondary mb-1">Site Key</label>
                  <input type="text" value={turnstileSiteKey} onChange={e => setTurnstileSiteKey(e.target.value)}
                    onBlur={() => save('turnstile_site_key', turnstileSiteKey)}
                    placeholder="0x4AAA..."
                    className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
                </div>
                <div>
                  <label className="block text-xs text-p-text-secondary mb-1">Secret Key {data?.turnstile_secret_key_set && <span className="text-green-600">(set)</span>}</label>
                  <input type="password" value={turnstileSecretKey} onChange={e => setTurnstileSecretKey(e.target.value)}
                    onBlur={() => { if (turnstileSecretKey) save('turnstile_secret_key', turnstileSecretKey) }}
                    placeholder={data?.turnstile_secret_key_set ? '••••••••' : 'Secret key'}
                    className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
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
