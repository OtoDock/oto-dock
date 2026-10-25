import { Fragment, useState, useEffect, useSyncExternalStore } from 'react'
import QRCode from 'qrcode'
import { isWakeDiagOn, setWakeDiag, subscribeWakeDiag } from '../audio/wakeDiag'
import { useAuth } from '../contexts/AuthContext'
import { fetchCurrentUser } from '../api/auth'
import { deletePasskey, listPasskeys, passkeySupported, registerPasskey, renamePasskey, type PasskeyInfo } from '../api/webauthn'

import { useActivityDisplay, setActivityDisplay, type ActivityDisplayMode } from '../hooks/useActivityDisplay'
import { useClearMyMemory } from '../api/memory'
import { useMyAudioPrefs, useUpdateMyAudioPrefs } from '../api/userAudio'
import { useMyUiPrefs, useUpdateMyUiPrefs } from '../api/userUiPrefs'
import { useWakeKeywords } from '../api/wakeWord'
import { useChatAudioCapability } from '../hooks/useChatAudioCapability'
import StrongConfirmModal from '../components/StrongConfirmModal'
import InvokedForm from '../components/common/InvokedForm'
import { useTheme } from '../contexts/useTheme'
import { errorText, errorName, apiOk, apiJson, withMessage, toastError } from '../lib/http/apiJson'
import Button from '../components/ui/Button'
import { Input } from '../components/ui/Field'
import { BUTTON_BASE, BUTTON_SIZE, CARD, CARD_BORDERED, SEGMENT } from '../components/ui/classes'
import InfoTip from '../components/ui/InfoTip'
import { CheckboxRow, SaveState } from '../components/ui/SettingsControls'
import { useSaveState } from '../hooks/useSaveState'

// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Profile Section (display name + email editing)
// ---------------------------------------------------------------------------

export function ProfileSection() {
  const { user, setUser } = useAuth()
  const isLocal = user?.auth_provider?.startsWith('local')

  const [displayName, setDisplayName] = useState(user?.display_name || '')
  const [email, setEmail] = useState(user?.email || '')
  const [emailPassword, setEmailPassword] = useState('')
  const [saving, setSaving] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const { stateOf, run } = useSaveState()

  // A profile change elsewhere replaces the drafts (in render, on the change).
  const [profile, setProfile] = useState({ name: user?.display_name, email: user?.email })
  if (profile.name !== user?.display_name || profile.email !== user?.email) {
    setProfile({ name: user?.display_name, email: user?.email })
    setDisplayName(user?.display_name || '')
    setEmail(user?.email || '')
  }

  // A failure keeps what was typed.
  function saveDisplayName() {
    if (!displayName.trim() || displayName === user?.display_name) return
    void run('name', async () => {
      await apiOk('/v1/users/me/profile', {
        method: 'PUT',
        body: JSON.stringify({ display_name: displayName.trim() }),
      }, 'Failed')
      const u = await fetchCurrentUser()
      if (u) setUser(u)
    }, { failTitle: 'The name was not saved' })
  }

  async function saveEmail() {
    if (!email.trim() || email === user?.email) return
    if (!emailPassword) { setError('Password required to change email'); return }
    setSaving('email'); setError(''); setSuccess('')
    try {
      await apiOk('/v1/users/me/email', {
        method: 'PUT',
        body: JSON.stringify({ new_email: email.trim(), password: emailPassword }),
      }, 'Failed')
      const u = await fetchCurrentUser()
      if (u) setUser(u)
      setEmailPassword('')
      setSuccess('email')
      setTimeout(() => setSuccess(''), 2000)
    } catch (err) { setError(errorText(err)) }
    finally { setSaving('') }
  }

  return (
    <div className="mb-8">
      <h2 className="font-display text-lg font-semibold text-primary mb-3">Profile</h2>
      {error && <div className="text-sm text-status-danger-fg mb-3">{error}<button onClick={() => setError('')} className="ml-2 underline">dismiss</button></div>}
      <div className={`${CARD} p-4 space-y-4`}>
        <div>
          <div className="mb-1">
            <label className="text-xs font-medium text-secondary">Display Name</label>
            <SaveState state={stateOf('name')} />
          </div>
          <div className="flex gap-2">
            <Input type="text" value={displayName} onChange={e => setDisplayName(e.target.value)}
              onBlur={saveDisplayName}
              className="flex-1" />
          </div>
        </div>

        <div>
          <label className="block text-xs font-medium text-secondary mb-1">
            Email{!isLocal && <span className="text-tertiary">, managed by your SSO provider</span>}
          </label>
          {isLocal ? (
            <div className="space-y-2">
              <Input type="email" value={email} onChange={e => setEmail(e.target.value)} />
              {email !== user?.email && (
                <div className="flex gap-2">
                  <Input type="password" value={emailPassword} onChange={e => setEmailPassword(e.target.value)}
                    placeholder="Confirm password to change email"
                    className="flex-1" />
                  <Button variant="primary" size="xs" onClick={saveEmail} disabled={saving === 'email'}>
                    {saving === 'email' ? 'Saving...' : 'Update Email'}
                  </Button>
                </div>
              )}
              {success === 'email' && <span className="text-xs text-status-success-fg">Email updated</span>}
            </div>
          ) : (
            <p className="text-sm text-primary px-3 py-2 bg-surface-sunken rounded-control">{user?.email}</p>
          )}
        </div>

        <div>
          <label className="block text-xs font-medium text-secondary mb-1">Role</label>
          <p className="text-sm text-primary px-3 py-2 bg-surface-sunken rounded-control capitalize">{user?.role}</p>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Security Section (local auth users only)
// ---------------------------------------------------------------------------

export function SecuritySection() {
  const { user } = useAuth()
  const [currentPw, setCurrentPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirmPw, setConfirmPw] = useState('')
  const [pwError, setPwError] = useState('')
  const [pwSuccess, setPwSuccess] = useState(false)
  const [pwLoading, setPwLoading] = useState(false)

  async function handleChangePassword(e: React.FormEvent) {
    e.preventDefault()
    setPwError(''); setPwSuccess(false)
    if (newPw !== confirmPw) { setPwError('Passwords do not match'); return }
    setPwLoading(true)
    try {
      await apiOk('/v1/users/me/password', {
        method: 'PUT',
        body: JSON.stringify({ current_password: currentPw, new_password: newPw }),
      }, 'Failed')
      setPwSuccess(true); setCurrentPw(''); setNewPw(''); setConfirmPw('')
    } catch (err) { setPwError(errorText(err)) }
    finally { setPwLoading(false) }
  }

  return (
    <div className="mb-8">
      <h2 className="font-display text-lg font-semibold text-primary mb-3">Security</h2>

      {/* Change Password */}
      <div className={`${CARD} p-4 mb-4`}>
        <h3 className="text-sm font-semibold text-primary mb-3">Change Password</h3>
        {pwError && <div className="text-sm text-status-danger-fg mb-3">{pwError}</div>}
        {pwSuccess && <div className="text-sm text-status-success-fg mb-3">Password changed successfully.</div>}
        <form onSubmit={handleChangePassword} className="space-y-3">
          {/* Hidden username field: silences the browser's "password form should
              have a username field" a11y warning and lets password managers
              associate the change with this account. */}
          <input type="text" name="username" autoComplete="username"
            value={user?.email || ''} readOnly hidden aria-hidden="true" tabIndex={-1} />
          <Input type="password" value={currentPw} onChange={e => setCurrentPw(e.target.value)}
            placeholder="Current password" required autoComplete="current-password" />
          <Input type="password" value={newPw} onChange={e => setNewPw(e.target.value)}
            placeholder="New password" required autoComplete="new-password" />
          <Input type="password" value={confirmPw} onChange={e => setConfirmPw(e.target.value)}
            placeholder="Confirm new password" required autoComplete="new-password" />
          <Button variant="primary" size="md" type="submit" disabled={pwLoading}>
            {pwLoading ? 'Changing...' : 'Change Password'}
          </Button>
        </form>
      </div>

      {/* Two-Factor Authentication — passkeys + authenticator app, unified */}
      <TwoFactorSection />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Two-Factor Section — passkeys + authenticator app, one card (local users)
// ---------------------------------------------------------------------------

export function TwoFactorSection() {
  const { user, setUser } = useAuth()
  const [passkeys, setPasskeys] = useState<PasskeyInfo[]>([])
  const [pkFeature, setPkFeature] = useState(false)
  const [pkRpHost, setPkRpHost] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')  // rendered INSIDE the open box

  // One flow open at a time.
  const [flow, setFlow] = useState<null | 'add-passkey' | 'add-totp' | 'disable-totp'>(null)
  const [addName, setAddName] = useState('')
  const [password, setPassword] = useState('')  // confirm field of the open box
  const [action, setAction] = useState<{ type: 'rename' | 'delete'; id: string } | null>(null)
  const [actionName, setActionName] = useState('')

  // TOTP setup flow
  const [totpSetup, setTotpSetup] = useState<{ secret: string; qr_uri: string; recovery_codes: string[] } | null>(null)
  const [totpCode, setTotpCode] = useState('')
  // Render the TOTP QR locally — the otpauth:// URI embeds the shared 2FA
  // secret, so it must never be sent to a third-party QR image service.
  // The QR image of the current secret (another secret shows none).
  const [qr, setQr] = useState<{ uri: string; url: string } | null>(null)
  const qrUri = totpSetup?.qr_uri
  const qrDataUrl = qr && qr.uri === qrUri ? qr.url : ''
  useEffect(() => {
    if (!qrUri) return
    let cancelled = false
    QRCode.toDataURL(qrUri, { width: 200, margin: 1 })
      .then((url) => { if (!cancelled) setQr({ uri: qrUri, url }) })
      .catch(() => { /* no image: the secret is shown as text */ })
    return () => { cancelled = true }
  }, [qrUri])

  async function refresh() {
    try {
      const data = await listPasskeys()
      setPasskeys(data.passkeys)
      setPkFeature(data.enabled)
      setPkRpHost(data.rp_host || '')
    } catch { /* list stays empty */ }
    finally { setLoaded(true) }
  }
  useEffect(() => {
    let alive = true
    listPasskeys()
      .then((data) => {
        if (!alive) return
        setPasskeys(data.passkeys)
        setPkFeature(data.enabled)
        setPkRpHost(data.rp_host || '')
      })
      .catch(() => { /* list stays empty */ })
      .finally(() => { if (alive) setLoaded(true) })
    return () => { alive = false }
  }, [])

  function openFlow(f: typeof flow) {
    setFlow(f); setAction(null); setError(''); setPassword(''); setAddName('')
  }
  function closeAll() {
    setFlow(null); setAction(null); setError(''); setPassword(''); setTotpSetup(null); setTotpCode('')
  }

  async function handleAddPasskey() {
    setBusy(true); setError('')
    try {
      await registerPasskey(password, addName.trim())
      closeAll()
      await refresh()
    } catch (err) {
      if (errorName(err) !== 'NotAllowedError') setError(errorText(err) || 'Failed to add passkey')
    } finally { setBusy(false) }
  }

  // `close` is the open box's own close, which gives the focus back (after a
  // removal, to the next passkey).
  async function handlePasskeyAction(close: (setClosed: () => void) => void) {
    if (!action) return
    setBusy(true); setError('')
    try {
      if (action.type === 'rename') await renamePasskey(action.id, actionName.trim(), password)
      else await deletePasskey(action.id, password)
      close(closeAll)
      await refresh()
    } catch (err) { setError(errorText(err) || 'Failed') }
    finally { setBusy(false) }
  }

  async function startTotpSetup() {
    setBusy(true); setError('')
    try {
      setTotpSetup(await apiJson<{ secret: string; qr_uri: string; recovery_codes: string[] }>(
        '/v1/users/me/totp/setup', { method: 'POST' },
      ).catch(withMessage('Failed to start setup')))
      setFlow('add-totp')
    } catch (err) { setError(errorText(err) || 'Failed to start setup') }
    finally { setBusy(false) }
  }

  async function verifyTotp() {
    setBusy(true); setError('')
    try {
      await apiOk('/v1/users/me/totp/verify', {
        method: 'POST',
        body: JSON.stringify({ code: totpCode }),
      }, 'Invalid code')
      const u = await fetchCurrentUser()
      if (u) setUser(u)
      closeAll()
    } catch (err) { setError(errorText(err)) }
    finally { setBusy(false) }
  }

  async function disableTotp() {
    setBusy(true); setError('')
    try {
      await apiOk('/v1/users/me/totp', {
        method: 'DELETE',
        body: JSON.stringify({ password }),
      }, 'Failed')
      const u = await fetchCurrentUser()
      if (u) setUser(u)
      closeAll()
    } catch (err) { setError(errorText(err)) }
    finally { setBusy(false) }
  }

  function formatWhen(iso: string | null): string {
    if (!iso) return 'never'
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
  }

  if (!loaded) return null

  const boxError = error && <div className="text-sm text-status-danger-fg">{error}</div>
  const nothingEnrolled = passkeys.length === 0 && !user?.totp_enabled
  // RP-bound: enrolling from an origin other than the configured public host
  // would fail in the browser — hide the button and explain instead.
  const pkRpMismatch = !!pkRpHost && window.location.hostname !== pkRpHost
  const canAddPasskey = pkFeature && passkeySupported() && !pkRpMismatch

  // The passkey rename or remove box (password-confirmed, its error shown
  // here) opens under the passkey it acts on.
  const passkeyBox = action && (
    <InvokedForm key={`${action.type}:${action.id}`} onCancel={closeAll} className="p-3 bg-surface-raised shadow-elevation-2 rounded-card space-y-2">
      {(close) => (
        <>
          <p className="text-xs text-secondary">
            {action.type === 'rename' ? 'Rename passkey' : 'Remove passkey'}. Confirm with your password.
          </p>
          {boxError}
          {action.type === 'rename' && (
            <Input type="text" value={actionName} onChange={e => setActionName(e.target.value)}
              placeholder="Passkey name" />
          )}
          <div className="flex gap-2">
            <Input type="password" value={password} onChange={e => setPassword(e.target.value)}
              placeholder="Password" autoComplete="current-password" className="flex-1" />
            <Button variant={action.type === 'delete' ? 'danger' : 'primary'} size="xs"
              onClick={() => { void handlePasskeyAction(close) }}
              disabled={busy || !password || (action.type === 'rename' && !actionName.trim())}>
              {busy ? 'Working...' : action.type === 'rename' ? 'Rename' : 'Remove'}
            </Button>
            <Button variant="ghost" size="xs" onClick={() => close(closeAll)}>Cancel</Button>
          </div>
        </>
      )}
    </InvokedForm>
  )

  return (
    <div className={`${CARD} p-4`}>
      <h3 className="text-sm font-semibold text-primary mb-1">Two-Factor Authentication</h3>
      <p className="text-xs text-secondary mb-3">
        {nothingEnrolled
          ? 'Add an extra layer of security with a passkey or an authenticator app.'
          : 'Your account is protected by a second factor.'}
      </p>

      <div className="space-y-3">
        {/* Enrolled factors — passkeys first, authenticator after */}
        {passkeys.map(pk => (
          <Fragment key={pk.credential_id}>
            <div className={`${CARD_BORDERED} flex items-center justify-between gap-3 px-3 py-2`}>
              <div className="flex items-center gap-2.5 min-w-0">
                <svg className="w-4 h-4 text-secondary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                </svg>
                <div className="min-w-0">
                  <p className="text-sm text-primary truncate">{pk.name}</p>
                  <p className="text-3xs text-tertiary">
                    Passkey · Added {formatWhen(pk.created_at)} · Last used {formatWhen(pk.last_used)}
                  </p>
                </div>
              </div>
              <div className="flex gap-2 shrink-0">
                <button onClick={() => { setAction({ type: 'rename', id: pk.credential_id }); setActionName(pk.name); setFlow(null); setPassword(''); setError('') }}
                  className="text-xs text-link hover:text-link-hover font-medium">Rename</button>
                <button onClick={() => { setAction({ type: 'delete', id: pk.credential_id }); setFlow(null); setPassword(''); setError('') }}
                  className="text-xs text-status-danger-fg hover:underline font-medium">Remove</button>
              </div>
            </div>
            {action?.id === pk.credential_id && passkeyBox}
          </Fragment>
        ))}
        {!!user?.totp_enabled && (
          <div className={`${CARD_BORDERED} flex items-center justify-between gap-3 px-3 py-2`}>
            <div className="flex items-center gap-2.5">
              <svg className="w-4 h-4 text-secondary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z" />
              </svg>
              <div>
                <p className="text-sm text-primary">Authenticator app</p>
                <p className="text-3xs text-tertiary">Time-based codes · recovery codes issued at setup</p>
              </div>
            </div>
            <button onClick={() => openFlow('disable-totp')}
              className="text-xs text-status-danger-fg hover:underline font-medium shrink-0">Remove</button>
          </div>
        )}

        {/* Add-passkey box */}
        {flow === 'add-passkey' && (
          <div className="p-3 bg-surface-raised shadow-elevation-2 rounded-card space-y-2">
            <p className="text-xs text-secondary">Confirm your password, then follow your browser's passkey prompt.</p>
            {boxError}
            <Input type="text" value={addName} onChange={e => setAddName(e.target.value)}
              placeholder="Name, e.g. This laptop" />
            <div className="flex gap-2">
              <Input type="password" value={password} onChange={e => setPassword(e.target.value)}
                placeholder="Password" autoComplete="current-password" className="flex-1" />
              <Button variant="primary" size="xs" onClick={handleAddPasskey} disabled={busy || !password}>
                {busy ? 'Waiting...' : 'Create passkey'}
              </Button>
              <Button variant="ghost" size="xs" onClick={closeAll}>Cancel</Button>
            </div>
          </div>
        )}

        {/* Authenticator setup box (QR → verify) */}
        {flow === 'add-totp' && totpSetup && (
          <div className="p-3 bg-surface-raised shadow-elevation-2 rounded-card space-y-3">
            {boxError}
            <p className="text-sm text-primary">Scan this QR code with your authenticator app:</p>
            {/* eslint-disable-next-line otodock/no-raw-palette -- a QR code keeps its light quiet zone in both themes */}
            <div className="bg-white p-4 rounded-card inline-block">
              {qrDataUrl ? <img src={qrDataUrl} alt="TOTP QR Code" className="w-48 h-48" /> : <div className="w-48 h-48" />}
            </div>
            <div>
              <p className="text-xs text-secondary mb-1">Or enter this key manually:</p>
              <code className="text-xs font-mono bg-surface-sunken px-2 py-1 rounded-sm select-all">{totpSetup.secret}</code>
            </div>
            <div className="p-3 bg-status-warning-bg border border-status-warning-border rounded-card">
              <p className="text-xs font-medium text-status-warning-fg mb-1">Recovery Codes</p>
              <p className="text-xs text-status-warning-fg mb-2">Save these codes in a safe place. Each can only be used once.</p>
              <div className="grid grid-cols-2 gap-1">
                {totpSetup.recovery_codes.map((code, i) => (
                  <code key={i} className="text-xs font-mono bg-surface-card px-2 py-1 rounded-sm">{code}</code>
                ))}
              </div>
            </div>
            <div className="flex gap-2">
              <Input type="text" value={totpCode} onChange={e => setTotpCode(e.target.value)}
                placeholder="000000" maxLength={6} autoComplete="one-time-code"
                className="max-w-32 text-center font-mono" />
              <Button variant="primary" size="md" onClick={verifyTotp} disabled={busy || totpCode.length !== 6}>
                {busy ? 'Verifying...' : 'Verify and enable'}
              </Button>
              <Button variant="ghost" size="xs" onClick={closeAll}>Cancel</Button>
            </div>
          </div>
        )}

        {/* Disable-authenticator box */}
        {flow === 'disable-totp' && (
          <div className="p-3 bg-surface-raised shadow-elevation-2 rounded-card space-y-2">
            <p className="text-xs text-secondary">Remove the authenticator app. Confirm with your password.</p>
            {boxError}
            <div className="flex gap-2">
              <Input type="password" value={password} onChange={e => setPassword(e.target.value)}
                placeholder="Password" autoComplete="current-password" className="flex-1" />
              <Button variant="danger" size="xs" onClick={disableTotp} disabled={busy || !password}>
                {busy ? 'Removing...' : 'Remove'}
              </Button>
              <Button variant="ghost" size="xs" onClick={closeAll}>Cancel</Button>
            </div>
          </div>
        )}

        {/* Add options — primary pair when nothing is enrolled, quiet links after */}
        {flow === null && !action && (
          nothingEnrolled ? (
            <div className="space-y-2">
              {error && <div className="text-sm text-status-danger-fg">{error}</div>}
              {canAddPasskey && (
                <Button variant="primary" size="md" onClick={() => openFlow('add-passkey')}
                  className="w-full">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                  </svg>
                  Add a passkey
                </Button>
              )}
              <Button variant={canAddPasskey ? 'secondary' : 'primary'} size="md" onClick={startTotpSetup} disabled={busy}
                className="w-full">
                {busy ? 'Setting up...' : 'Use an authenticator app'}
              </Button>
              {!pkFeature && (
                <p className="text-3xs text-tertiary">
                  Passkeys need OtoDock on a secure https:// address. Ask your administrator.
                </p>
              )}
              {pkRpMismatch && (
                <p className="text-3xs text-tertiary">
                  Passkeys can only be added and used at https://{pkRpHost}. Open OtoDock there.
                </p>
              )}
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-3">
              {error && <div className="w-full text-sm text-status-danger-fg">{error}</div>}
              {canAddPasskey && (
                <button onClick={() => openFlow('add-passkey')}
                  className="text-xs font-medium text-link hover:text-link-hover">+ Add another passkey</button>
              )}
              {!user?.totp_enabled && (
                <button onClick={startTotpSetup} disabled={busy}
                  className="text-xs font-medium text-link hover:text-link-hover disabled:opacity-50">+ Set up an authenticator app</button>
              )}
              {pkRpMismatch && (
                <p className="w-full text-3xs text-tertiary">
                  Passkeys can only be added and used at https://{pkRpHost}. Open OtoDock there.
                </p>
              )}
            </div>
          )
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Appearance Section (theme picker)
// ---------------------------------------------------------------------------

export function AppearanceSection() {
  const { theme, setTheme } = useTheme()
  const activityDisplay = useActivityDisplay()

  const activityOptions: { value: ActivityDisplayMode; label: string; icon: React.ReactNode }[] = [
    {
      value: 'compact',
      label: 'Compact',
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h10M4 18h16" />
        </svg>
      ),
    },
    {
      value: 'detailed',
      label: 'Detailed',
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 10h16M4 14h16M4 18h16" />
        </svg>
      ),
    },
  ]

  const themeOptions: { value: 'system' | 'light' | 'dark'; label: string; icon: React.ReactNode }[] = [
    {
      value: 'system',
      label: 'System',
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
        </svg>
      ),
    },
    {
      value: 'light',
      label: 'Light',
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
        </svg>
      ),
    },
    {
      value: 'dark',
      label: 'Dark',
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
        </svg>
      ),
    },
  ]

  return (
    <div className="mb-8">
      <h2 className="font-display text-lg font-semibold text-primary mb-3">Appearance</h2>
      <p className="text-sm text-secondary mb-4">
        Choose how the dashboard looks.
      </p>
      <div role="group" aria-label="Theme" className="flex flex-wrap gap-2">
        {themeOptions.map(opt => (
          <button
            key={opt.value}
            type="button"
            onClick={() => setTheme(opt.value)}
            aria-pressed={theme === opt.value}
            className={`${BUTTON_BASE} ${BUTTON_SIZE['md']} ${theme === opt.value ? SEGMENT.on : SEGMENT.off}`}
          >
            {opt.icon}
            {opt.label}
          </button>
        ))}
      </div>
      <div className="mt-6">
        <div className="mb-3">
          <h3 className="inline text-sm font-medium text-primary">Chat activity</h3>
          <InfoTip label="Chat activity">Compact folds tool activity into expandable summaries.</InfoTip>
        </div>
        <div role="group" aria-label="Chat activity" className="flex flex-wrap gap-2">
          {activityOptions.map(opt => (
            <button
              key={opt.value}
              type="button"
              onClick={() => setActivityDisplay(opt.value)}
              aria-pressed={activityDisplay === opt.value}
              className={`${BUTTON_BASE} ${BUTTON_SIZE['md']} ${activityDisplay === opt.value ? SEGMENT.on : SEGMENT.off}`}
            >
              {opt.icon}
              {opt.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function WakeWordSection() {
  const { data: prefs } = useMyAudioPrefs()
  const update = useUpdateMyAudioPrefs()
  const { data: capability } = useChatAudioCapability()
  const enabled = prefs?.wake_word_enabled === true
  const { data: kw } = useWakeKeywords(enabled)
  const duplexOk = capability?.duplex?.available === true

  const wakeable = (kw?.agents ?? []).filter((a) => a.wakeable)
  const platformTarget = kw?.agents?.find((a) => a.slug === kw?.platform_target)
  const { stateOf, run } = useSaveState()

  return (
    <div className="mb-8">
      <div className="mb-3">
        <h2 className="inline font-display text-lg font-semibold text-primary">Wake word</h2>
        <InfoTip label="Wake word">
          To start a voice conversation, say “Hey OtoDock” on any dashboard
          page. You can also say “Hey” plus an agent’s name. Detection runs
          only on this device. Audio never leaves it until a conversation
          starts.
        </InfoTip>
        <SaveState state={stateOf('wake_word')} />
      </div>
      <p className="text-sm text-secondary mb-3">
        The microphone is only on while the dashboard is open.
      </p>
      <div role="group" aria-label="Wake word" className="flex gap-2">
        {([false, true] as const).map((value) => (
          <button
            key={String(value)}
            type="button"
            onClick={() => { void run('wake_word', () => update.mutateAsync({ wake_word_enabled: value }), { failTitle: 'The wake word setting was not saved' }) }}
            aria-pressed={enabled === value}
            className={`${BUTTON_BASE} ${BUTTON_SIZE['md']} ${enabled === value ? SEGMENT.on : SEGMENT.off}`}
          >
            {value ? 'On' : 'Off'}
          </button>
        ))}
      </div>
      {enabled && !duplexOk && (
        <p className="text-xs text-tertiary mt-2">
          Voice conversations are currently unavailable
          {capability?.duplex?.reason ? `: ${capability.duplex.reason}` : ''}.
          The wake word will turn on once they are back.
        </p>
      )}
      {enabled && duplexOk && kw?.enabled === false && (
        <p className="text-xs text-tertiary mt-2">
          Wake word is not active
          {kw?.reason ? ` (${kw.reason})` : ''}. Add an agent to your
          account and it will start listening.
        </p>
      )}
      {enabled && kw?.enabled && (
        <p className="text-xs text-tertiary mt-2">
          {platformTarget
            ? <>“Hey OtoDock” opens your favorite agent, {platformTarget.display_name}. </>
            : null}
          {wakeable.length > 0 && (
            <>Also listening for: {wakeable.map((a) => `“Hey ${a.display_name}”`).join(', ')}.</>
          )}
        </p>
      )}
      {enabled && <WakeDiagControl />}
    </div>
  )
}

// Troubleshooting switch for the wake word: keeps the last 30 seconds of
// what the listener hears in memory (audio/wakeDiag.ts) so a missed phrase
// can be saved to a local file and replayed offline. Lives here because the
// phone app has no address bar for the `?wakeDiag=1` shortcut — folded
// under a closed "Troubleshooting" disclosure (operator 2026-09-11): a rare
// control must not read as something every user should consider. It stays
// open while a recording is on so Stop is always reachable.
function WakeDiagControl() {
  const on = useSyncExternalStore(subscribeWakeDiag, isWakeDiagOn, () => false)
  const [open, setOpen] = useState(false)
  const expanded = open || on
  return (
    <div className="mt-4">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-xs text-tertiary hover:text-primary transition-colors"
      >
        <svg
          viewBox="0 0 16 16"
          className={`h-3 w-3 transition-transform ${expanded ? 'rotate-90' : ''}`}
          fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
        >
          <path d="M6 3l5 5-5 5" />
        </svg>
        Troubleshooting
      </button>
      {expanded && (
        <div className="mt-2">
          <Button variant="secondary" size="sm"
            type="button"
            onClick={() => setWakeDiag(!on)}
          >
            {on ? 'Stop recording diagnostics' : 'Record diagnostics'}
          </Button>
          <p className="text-xs text-tertiary mt-2">
            {on
              ? 'Recording. A small badge at the bottom right shows what the listener is doing. Say the wake phrase once and wait a few seconds. Then press Save on the badge. It saves the last 30 seconds and the event log in one local file. Nothing is uploaded.'
              : 'Helps when the wake phrase is missed. It keeps the last 30 seconds the listener hears, on this device only. It holds them until you save them to a file or stop.'}
          </p>
        </div>
      )}
    </div>
  )
}

function MyMemorySection() {
  const clear = useClearMyMemory()
  const [confirming, setConfirming] = useState(false)
  const [lastResult, setLastResult] = useState<{ files: number; agents: number } | null>(null)
  return (
    <div className="mb-8">
      <h2 className="font-display text-lg font-semibold text-primary mb-3">Memory</h2>
      <p className="text-sm text-secondary mb-3">
        Personal memories agents have captured about you, like your preferences and patterns.
        Clearing deletes your memory topic files across every agent.
      </p>
      <Button variant="danger" size="sm"
        onClick={() => setConfirming(true)}
      >
        Clear my memory across all agents
      </Button>
      {lastResult !== null && (
        <p className="text-xs text-tertiary mt-2">
          Cleared {lastResult.files} memory {lastResult.files === 1 ? 'file' : 'files'}
          {' '}across {lastResult.agents} {lastResult.agents === 1 ? 'agent' : 'agents'}.
        </p>
      )}
      {confirming && (
        <StrongConfirmModal
          title="Clear my memory"
          description={
            <>
              Delete your memory topic files across every agent. An admin can
              still recover them from the agent's Git history.
            </>
          }
          confirmWord="CLEAR-MY-MEMORY"
          confirmLabel="Clear memory"
          busyLabel="Clearing…"
          isPending={clear.isPending}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            clear.mutateAsync(undefined)
              .then((r) => {
                setLastResult({ files: r.files_unlinked, agents: r.agents_affected })
                setConfirming(false)
              })
              .catch(toastError('The memory was not cleared'))
          }}
        />
      )}
    </div>
  )
}

export { MyMemorySection, WakeWordSection }


// ---------------------------------------------------------------------------
// Notifications — the owner's switch for the subscription window alerts
// ---------------------------------------------------------------------------

export function NotificationsSection() {
  const { data: prefs } = useMyUiPrefs()
  const update = useUpdateMyUiPrefs()
  // Absent means on: the alerts ship enabled and the bag only stores an opt-out.
  const alertsOn = prefs?.subscription_usage_alerts !== false
  const { stateOf, run } = useSaveState()

  return (
    <div className="mb-8">
      <h2 className="font-display text-lg font-semibold text-primary mb-3">Notifications</h2>
      <CheckboxRow
        label="Subscription usage alerts"
        checked={alertsOn}
        save={stateOf('alerts')}
        onChange={(on) => { void run('alerts', () => update.mutateAsync({ subscription_usage_alerts: on }), { failTitle: 'The alert setting was not saved' }) }}
        info={<>
          This watches your connected Claude and ChatGPT accounts. You get a notification when
          one passes 90% of its weekly limit. You get another when it reaches the limit.
        </>}
      />
    </div>
  )
}


// ---------------------------------------------------------------------------
// Apps — what agents may know and do about the apps on this user's screen
// ---------------------------------------------------------------------------

export function AppsSection() {
  const { data: prefs } = useMyUiPrefs()
  const update = useUpdateMyUiPrefs()
  // Absent means on for both: the bag only stores an opt-out.
  const focusOn = prefs?.share_focus_with_agents !== false
  const openOn = prefs?.agents_may_open_apps !== false
  const { stateOf, run } = useSaveState()

  return (
    <div className="mb-8" data-testid="settings-apps">
      <h2 className="font-display text-lg font-semibold text-primary mb-3">Apps</h2>
      <div className="space-y-3">
        <CheckboxRow
          label="Agents know which app I am looking at"
          checked={focusOn}
          save={stateOf('focus')}
          onChange={(on) => { void run('focus', () => update.mutateAsync({ share_focus_with_agents: on }), { failTitle: 'The app setting was not saved' }) }}
          description="Nobody else learns what you are looking at."
          info={<>
            While an app is on your screen, the agent you talk to learns its name. So
            "update this" needs no explanation.
          </>}
        />
        <CheckboxRow
          label="Agents may open apps on my screen"
          checked={openOn}
          save={stateOf('open')}
          onChange={(on) => { void run('open', () => update.mutateAsync({ agents_may_open_apps: on }), { failTitle: 'The app setting was not saved' }) }}
          info={<>
            An agent can bring up one of its apps for you. In the chat you are in, it opens right
            away. Anywhere else, you get a notice with an Open button.
          </>}
        />
      </div>
    </div>
  )
}
