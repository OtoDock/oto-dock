import { useState, useEffect } from 'react'
import QRCode from 'qrcode'
import { useAuth } from '../contexts/AuthContext'
import { apiFetch, fetchCurrentUser } from '../api/auth'
import { deletePasskey, listPasskeys, passkeySupported, registerPasskey, renamePasskey, type PasskeyInfo } from '../api/webauthn'
import { useTheme } from '../contexts/ThemeContext'
import { useClearMyMemory } from '../api/memory'
import StrongConfirmModal from '../components/StrongConfirmModal'

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

  useEffect(() => {
    setDisplayName(user?.display_name || '')
    setEmail(user?.email || '')
  }, [user?.display_name, user?.email])

  async function saveDisplayName() {
    if (!displayName.trim() || displayName === user?.display_name) return
    setSaving('name'); setError(''); setSuccess('')
    try {
      const res = await apiFetch('/v1/users/me/profile', {
        method: 'PUT',
        body: JSON.stringify({ display_name: displayName.trim() }),
      })
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || 'Failed') }
      const u = await fetchCurrentUser()
      if (u) setUser(u)
      setSuccess('name')
      setTimeout(() => setSuccess(''), 2000)
    } catch (err: any) { setError(err.message) }
    finally { setSaving('') }
  }

  async function saveEmail() {
    if (!email.trim() || email === user?.email) return
    if (!emailPassword) { setError('Password required to change email'); return }
    setSaving('email'); setError(''); setSuccess('')
    try {
      const res = await apiFetch('/v1/users/me/email', {
        method: 'PUT',
        body: JSON.stringify({ new_email: email.trim(), password: emailPassword }),
      })
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || 'Failed') }
      const u = await fetchCurrentUser()
      if (u) setUser(u)
      setEmailPassword('')
      setSuccess('email')
      setTimeout(() => setSuccess(''), 2000)
    } catch (err: any) { setError(err.message) }
    finally { setSaving('') }
  }

  return (
    <div className="mb-8">
      <h2 className="text-lg font-medium text-p-text mb-3">Profile</h2>
      {error && <div className="text-sm text-p-accent-red mb-3">{error}<button onClick={() => setError('')} className="ml-2 underline">dismiss</button></div>}
      <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-4 space-y-4">
        <div>
          <label className="block text-xs font-medium text-p-text-secondary mb-1">Display Name</label>
          <div className="flex gap-2">
            <input type="text" value={displayName} onChange={e => setDisplayName(e.target.value)}
              onBlur={saveDisplayName}
              className="flex-1 px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
            {success === 'name' && <span className="text-xs text-green-600 dark:text-green-400 self-center animate-pulse">Saved</span>}
            {saving === 'name' && <span className="text-xs text-p-text-secondary self-center">Saving...</span>}
          </div>
        </div>

        <div>
          <label className="block text-xs font-medium text-p-text-secondary mb-1">
            Email {!isLocal && <span className="text-p-text-light">(managed by SSO provider)</span>}
          </label>
          {isLocal ? (
            <div className="space-y-2">
              <input type="email" value={email} onChange={e => setEmail(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
              {email !== user?.email && (
                <div className="flex gap-2">
                  <input type="password" value={emailPassword} onChange={e => setEmailPassword(e.target.value)}
                    placeholder="Confirm password to change email"
                    className="flex-1 px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
                  <button onClick={saveEmail} disabled={saving === 'email'}
                    className="px-3 py-2 text-xs font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50">
                    {saving === 'email' ? 'Saving...' : 'Update Email'}
                  </button>
                </div>
              )}
              {success === 'email' && <span className="text-xs text-green-600 dark:text-green-400 animate-pulse">Email updated</span>}
            </div>
          ) : (
            <p className="text-sm text-p-text px-3 py-2 bg-p-bg rounded-lg">{user?.email}</p>
          )}
        </div>

        <div>
          <label className="block text-xs font-medium text-p-text-secondary mb-1">Role</label>
          <p className="text-sm text-p-text px-3 py-2 bg-p-bg rounded-lg capitalize">{user?.role}</p>
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
      const res = await apiFetch('/v1/users/me/password', {
        method: 'PUT',
        body: JSON.stringify({ current_password: currentPw, new_password: newPw }),
      })
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || 'Failed') }
      setPwSuccess(true); setCurrentPw(''); setNewPw(''); setConfirmPw('')
    } catch (err: any) { setPwError(err.message) }
    finally { setPwLoading(false) }
  }

  return (
    <div className="mb-8">
      <h2 className="text-lg font-medium text-p-text mb-3">Security</h2>

      {/* Change Password */}
      <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-4 mb-4">
        <h3 className="text-sm font-semibold text-p-text mb-3">Change Password</h3>
        {pwError && <div className="text-sm text-p-accent-red mb-3">{pwError}</div>}
        {pwSuccess && <div className="text-sm text-green-600 dark:text-green-400 mb-3">Password changed successfully.</div>}
        <form onSubmit={handleChangePassword} className="space-y-3">
          {/* Hidden username field: silences the browser's "password form should
              have a username field" a11y warning and lets password managers
              associate the change with this account. */}
          <input type="text" name="username" autoComplete="username"
            value={user?.email || ''} readOnly hidden aria-hidden="true" tabIndex={-1} />
          <input type="password" value={currentPw} onChange={e => setCurrentPw(e.target.value)}
            placeholder="Current password" required autoComplete="current-password"
            className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
          <input type="password" value={newPw} onChange={e => setNewPw(e.target.value)}
            placeholder="New password" required autoComplete="new-password"
            className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
          <input type="password" value={confirmPw} onChange={e => setConfirmPw(e.target.value)}
            placeholder="Confirm new password" required autoComplete="new-password"
            className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
          <button type="submit" disabled={pwLoading}
            className="px-4 py-2 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50">
            {pwLoading ? 'Changing...' : 'Change Password'}
          </button>
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

const TF_INPUT = 'px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30'

export function TwoFactorSection() {
  const { user, setUser } = useAuth()
  const [passkeys, setPasskeys] = useState<PasskeyInfo[]>([])
  const [pkFeature, setPkFeature] = useState(false)
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
  const [qrDataUrl, setQrDataUrl] = useState('')
  useEffect(() => {
    if (!totpSetup?.qr_uri) { setQrDataUrl(''); return }
    let cancelled = false
    QRCode.toDataURL(totpSetup.qr_uri, { width: 200, margin: 1 })
      .then((url) => { if (!cancelled) setQrDataUrl(url) })
      .catch(() => { if (!cancelled) setQrDataUrl('') })
    return () => { cancelled = true }
  }, [totpSetup?.qr_uri])

  async function refresh() {
    try {
      const data = await listPasskeys()
      setPasskeys(data.passkeys)
      setPkFeature(data.enabled)
    } catch { /* list stays empty */ }
    finally { setLoaded(true) }
  }
  useEffect(() => { refresh() }, [])

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
    } catch (err: any) {
      if (err?.name !== 'NotAllowedError') setError(err.message || 'Failed to add passkey')
    } finally { setBusy(false) }
  }

  async function handlePasskeyAction() {
    if (!action) return
    setBusy(true); setError('')
    try {
      if (action.type === 'rename') await renamePasskey(action.id, actionName.trim(), password)
      else await deletePasskey(action.id, password)
      closeAll()
      await refresh()
    } catch (err: any) { setError(err.message || 'Failed') }
    finally { setBusy(false) }
  }

  async function startTotpSetup() {
    setBusy(true); setError('')
    try {
      const res = await apiFetch('/v1/users/me/totp/setup', { method: 'POST' })
      if (!res.ok) throw new Error('Failed to start setup')
      setTotpSetup(await res.json())
      setFlow('add-totp')
    } catch (err: any) { setError(err.message || 'Failed to start setup') }
    finally { setBusy(false) }
  }

  async function verifyTotp() {
    setBusy(true); setError('')
    try {
      const res = await apiFetch('/v1/users/me/totp/verify', {
        method: 'POST',
        body: JSON.stringify({ code: totpCode }),
      })
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || 'Invalid code') }
      const u = await fetchCurrentUser()
      if (u) setUser(u)
      closeAll()
    } catch (err: any) { setError(err.message) }
    finally { setBusy(false) }
  }

  async function disableTotp() {
    setBusy(true); setError('')
    try {
      const res = await apiFetch('/v1/users/me/totp', {
        method: 'DELETE',
        body: JSON.stringify({ password }),
      })
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || 'Failed') }
      const u = await fetchCurrentUser()
      if (u) setUser(u)
      closeAll()
    } catch (err: any) { setError(err.message) }
    finally { setBusy(false) }
  }

  function formatWhen(iso: string | null): string {
    if (!iso) return 'never'
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
  }

  if (!loaded) return null

  const boxError = error && <div className="text-sm text-p-accent-red">{error}</div>
  const nothingEnrolled = passkeys.length === 0 && !user?.totp_enabled
  const canAddPasskey = pkFeature && passkeySupported()

  return (
    <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-4">
      <h3 className="text-sm font-semibold text-p-text mb-1">Two-Factor Authentication</h3>
      <p className="text-xs text-p-text-secondary mb-3">
        {nothingEnrolled
          ? 'Add an extra layer of security with a passkey or an authenticator app.'
          : 'Your account is protected by a second factor.'}
      </p>

      <div className="space-y-3">
        {/* Enrolled factors — passkeys first, authenticator after */}
        {passkeys.map(pk => (
          <div key={pk.credential_id} className="flex items-center justify-between gap-3 px-3 py-2 border border-p-border-light rounded-lg">
            <div className="flex items-center gap-2.5 min-w-0">
              <svg className="w-4 h-4 text-brand shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
              </svg>
              <div className="min-w-0">
                <p className="text-sm text-p-text truncate">{pk.name}</p>
                <p className="text-[10px] text-p-text-light">
                  Passkey · Added {formatWhen(pk.created_at)} · Last used {formatWhen(pk.last_used)}
                </p>
              </div>
            </div>
            <div className="flex gap-2 shrink-0">
              <button onClick={() => { setAction({ type: 'rename', id: pk.credential_id }); setActionName(pk.name); setFlow(null); setPassword(''); setError('') }}
                className="text-xs text-brand hover:text-brand-hover font-medium">Rename</button>
              <button onClick={() => { setAction({ type: 'delete', id: pk.credential_id }); setFlow(null); setPassword(''); setError('') }}
                className="text-xs text-p-accent-red hover:underline font-medium">Remove</button>
            </div>
          </div>
        ))}
        {!!user?.totp_enabled && (
          <div className="flex items-center justify-between gap-3 px-3 py-2 border border-p-border-light rounded-lg">
            <div className="flex items-center gap-2.5">
              <svg className="w-4 h-4 text-brand shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z" />
              </svg>
              <div>
                <p className="text-sm text-p-text">Authenticator app</p>
                <p className="text-[10px] text-p-text-light">Time-based codes · recovery codes issued at setup</p>
              </div>
            </div>
            <button onClick={() => openFlow('disable-totp')}
              className="text-xs text-p-accent-red hover:underline font-medium shrink-0">Remove</button>
          </div>
        )}

        {/* Passkey rename/remove box (password-confirmed; error shown HERE) */}
        {action && (
          <div className="p-3 bg-p-bg rounded-lg space-y-2">
            <p className="text-xs text-p-text-secondary">
              {action.type === 'rename' ? 'Rename passkey' : 'Remove passkey'} — confirm with your password.
            </p>
            {boxError}
            {action.type === 'rename' && (
              <input type="text" value={actionName} onChange={e => setActionName(e.target.value)}
                placeholder="Passkey name" className={`w-full ${TF_INPUT}`} />
            )}
            <div className="flex gap-2">
              <input type="password" value={password} onChange={e => setPassword(e.target.value)}
                placeholder="Password" autoComplete="current-password" className={`flex-1 ${TF_INPUT}`} />
              <button onClick={handlePasskeyAction}
                disabled={busy || !password || (action.type === 'rename' && !actionName.trim())}
                className={`px-3 py-2 text-xs font-medium rounded-lg disabled:opacity-50 ${
                  action.type === 'delete'
                    ? 'border border-p-accent-red text-p-accent-red hover:bg-red-50 dark:hover:bg-red-900/20'
                    : 'text-white bg-brand hover:bg-brand-hover'
                }`}>
                {busy ? 'Working...' : action.type === 'rename' ? 'Rename' : 'Remove'}
              </button>
              <button onClick={closeAll} className="px-3 py-2 text-xs text-p-text-secondary hover:text-p-text">Cancel</button>
            </div>
          </div>
        )}

        {/* Add-passkey box */}
        {flow === 'add-passkey' && (
          <div className="p-3 bg-p-bg rounded-lg space-y-2">
            <p className="text-xs text-p-text-secondary">Confirm your password, then follow your browser's passkey prompt.</p>
            {boxError}
            <input type="text" value={addName} onChange={e => setAddName(e.target.value)}
              placeholder="Name (e.g. This laptop)" className={`w-full ${TF_INPUT}`} />
            <div className="flex gap-2">
              <input type="password" value={password} onChange={e => setPassword(e.target.value)}
                placeholder="Password" autoComplete="current-password" className={`flex-1 ${TF_INPUT}`} />
              <button onClick={handleAddPasskey} disabled={busy || !password}
                className="px-3 py-2 text-xs font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50">
                {busy ? 'Waiting...' : 'Create passkey'}
              </button>
              <button onClick={closeAll} className="px-3 py-2 text-xs text-p-text-secondary hover:text-p-text">Cancel</button>
            </div>
          </div>
        )}

        {/* Authenticator setup box (QR → verify) */}
        {flow === 'add-totp' && totpSetup && (
          <div className="p-3 bg-p-bg rounded-lg space-y-3">
            {boxError}
            <p className="text-sm text-p-text">Scan this QR code with your authenticator app:</p>
            <div className="bg-white p-4 rounded-lg inline-block border border-p-border-light">
              {qrDataUrl ? <img src={qrDataUrl} alt="TOTP QR Code" className="w-48 h-48" /> : <div className="w-48 h-48" />}
            </div>
            <div>
              <p className="text-xs text-p-text-secondary mb-1">Or enter this key manually:</p>
              <code className="text-xs font-mono bg-white dark:bg-p-surface px-2 py-1 rounded-sm select-all">{totpSetup.secret}</code>
            </div>
            <div className="p-3 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg">
              <p className="text-xs font-medium text-amber-800 dark:text-amber-300 mb-1">Recovery Codes</p>
              <p className="text-xs text-amber-700 dark:text-amber-400 mb-2">Save these codes in a safe place. Each can only be used once.</p>
              <div className="grid grid-cols-2 gap-1">
                {totpSetup.recovery_codes.map((code, i) => (
                  <code key={i} className="text-xs font-mono bg-white dark:bg-p-surface px-2 py-1 rounded-sm">{code}</code>
                ))}
              </div>
            </div>
            <div className="flex gap-2">
              <input type="text" value={totpCode} onChange={e => setTotpCode(e.target.value)}
                placeholder="000000" maxLength={6} autoComplete="one-time-code"
                className={`w-32 text-center font-mono ${TF_INPUT}`} />
              <button onClick={verifyTotp} disabled={busy || totpCode.length !== 6}
                className="px-4 py-2 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50">
                {busy ? 'Verifying...' : 'Verify & enable'}
              </button>
              <button onClick={closeAll} className="px-3 py-2 text-xs text-p-text-secondary hover:text-p-text">Cancel</button>
            </div>
          </div>
        )}

        {/* Disable-authenticator box */}
        {flow === 'disable-totp' && (
          <div className="p-3 bg-p-bg rounded-lg space-y-2">
            <p className="text-xs text-p-text-secondary">Remove the authenticator app — confirm with your password.</p>
            {boxError}
            <div className="flex gap-2">
              <input type="password" value={password} onChange={e => setPassword(e.target.value)}
                placeholder="Password" autoComplete="current-password" className={`flex-1 ${TF_INPUT}`} />
              <button onClick={disableTotp} disabled={busy || !password}
                className="px-3 py-2 text-xs font-medium border border-p-accent-red text-p-accent-red rounded-lg hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50">
                {busy ? 'Removing...' : 'Remove'}
              </button>
              <button onClick={closeAll} className="px-3 py-2 text-xs text-p-text-secondary hover:text-p-text">Cancel</button>
            </div>
          </div>
        )}

        {/* Add options — primary pair when nothing is enrolled, quiet links after */}
        {flow === null && !action && (
          nothingEnrolled ? (
            <div className="space-y-2">
              {error && <div className="text-sm text-p-accent-red">{error}</div>}
              {canAddPasskey && (
                <button onClick={() => openFlow('add-passkey')}
                  className="w-full flex items-center justify-center gap-2 px-4 py-2 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                  </svg>
                  Add a passkey
                </button>
              )}
              <button onClick={startTotpSetup} disabled={busy}
                className={`w-full px-4 py-2 text-sm font-medium rounded-lg disabled:opacity-50 ${
                  canAddPasskey
                    ? 'border border-p-border-light text-p-text hover:bg-p-surface-hover'
                    : 'text-white bg-brand hover:bg-brand-hover'
                }`}>
                {busy ? 'Setting up...' : 'Use an authenticator app'}
              </button>
              {!pkFeature && (
                <p className="text-[10px] text-p-text-light">
                  Passkeys need this platform to be served from an HTTPS address — ask your administrator.
                </p>
              )}
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-3">
              {error && <div className="w-full text-sm text-p-accent-red">{error}</div>}
              {canAddPasskey && (
                <button onClick={() => openFlow('add-passkey')}
                  className="text-xs font-medium text-brand hover:text-brand-hover">+ Add another passkey</button>
              )}
              {!user?.totp_enabled && (
                <button onClick={startTotpSetup} disabled={busy}
                  className="text-xs font-medium text-brand hover:text-brand-hover disabled:opacity-50">+ Set up an authenticator app</button>
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
      <h2 className="text-lg font-medium text-p-text mb-3">Appearance</h2>
      <p className="text-sm text-p-text-secondary mb-4">
        Choose how the dashboard looks.
      </p>
      <div className="flex gap-2">
        {themeOptions.map(opt => (
          <button
            key={opt.value}
            onClick={() => setTheme(opt.value)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              theme === opt.value
                ? 'bg-brand text-white'
                : 'bg-white dark:bg-p-surface border border-p-border-light text-p-text-secondary hover:text-p-text'
            }`}
          >
            {opt.icon}
            {opt.label}
          </button>
        ))}
      </div>
    </div>
  )
}

function MyMemorySection() {
  const clear = useClearMyMemory()
  const [confirming, setConfirming] = useState(false)
  const [lastResult, setLastResult] = useState<{ files: number; agents: number } | null>(null)
  return (
    <div className="mb-8">
      <h2 className="text-lg font-medium text-p-text mb-3">Memory</h2>
      <p className="text-sm text-p-text-secondary mb-3">
        Personal memories agents have captured about you (preferences, patterns).
        Clearing deletes your memory topic files across every agent.
      </p>
      <button
        onClick={() => setConfirming(true)}
        className="px-3 py-1.5 text-sm rounded-lg border border-red-300 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
      >
        Clear my memory across all agents
      </button>
      {lastResult !== null && (
        <p className="text-xs text-p-text-light mt-2">
          Cleared {lastResult.files} memory {lastResult.files === 1 ? 'file' : 'files'}
          {' '}across {lastResult.agents} {lastResult.agents === 1 ? 'agent' : 'agents'}.
        </p>
      )}
      {confirming && (
        <StrongConfirmModal
          title="Clear my memory"
          description={
            <>
              Delete your memory topic files across every agent. Git history is
              preserved — an admin can recover the file via the per-agent
              Git History tab.
            </>
          }
          confirmWord="CLEAR-MY-MEMORY"
          confirmLabel="Clear memory"
          busyLabel="Clearing…"
          isPending={clear.isPending}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            clear.mutate(undefined, {
              onSuccess: (r) => {
                setLastResult({ files: r.files_unlinked, agents: r.agents_affected })
                setConfirming(false)
              },
            })
          }}
        />
      )}
    </div>
  )
}

export { MyMemorySection }
