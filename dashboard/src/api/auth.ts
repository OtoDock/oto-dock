// Authentication API — local + OIDC

export interface User {
  sub: string
  email: string
  name: string
  display_name?: string
  role: 'admin' | 'creator' | 'member'
  agents: string[]
  default_agent?: string
  agent_roles: Record<string, 'manager' | 'editor' | 'viewer'>
  platform_configured?: boolean
  // Whether this user has connected their OWN AI engine (personal Claude Code
  // or Codex subscription). Drives the per-user "connect an AI engine" banner —
  // distinct from platform_configured (which is true if they can merely borrow).
  has_own_engine?: boolean
  auth_provider?: string
  totp_enabled?: boolean
  is_owner?: boolean
  must_change_password?: boolean
  // Admin require-2FA policy: local account without a second factor —
  // the dashboard forces the enrollment screen until one is set up.
  must_enroll_2fa?: boolean
  // User-facing feature flags surfaced by /auth/me so the dashboard can
  // hide gated sections (e.g. Remote Machines).
  feature_flags?: {
    allow_user_paired_machines?: boolean
    // False when this build ships without the satellite source tree —
    // the whole Remote Machines feature (pairing, targeting UI) is hidden.
    remote_machines_available?: boolean
    // Mirrors the platform-wide interactive kill-switch: when off, the
    // interactive-terminal toggles are hidden (sessions always run headless).
    interactive_terminal_enabled?: boolean
  }
}

export interface AuthConfig {
  oidc_enabled: boolean
  oidc_provider_name: string
  turnstile_site_key: string
  setup_required: boolean
  auth_provider_bypass: boolean
  smtp_configured: boolean
  // Emailed links (password reset, invite): SMTP AND a public dashboard URL —
  // without the URL an emailed relative link wouldn't resolve.
  email_links_available: boolean
  password_min_score: number
  password_min_length: number
  // Passkeys (WebAuthn) — on only for https public-URL installs
  passkeys_enabled: boolean
  // 'passwordless' (primary sign-in button) or 'second_factor' (passkeys
  // offered only at the 2FA step after a correct password)
  passkey_login_mode: string
  // The RP hostname passkeys are bound to (empty when passkeys are off). On
  // any other origin the browser refuses the ceremony — the login page hides
  // its passkey buttons and points at this host instead.
  passkey_rp_host: string
  // OtoDock connectivity + deployment. `air_gapped` (effective — forced false on
  // cloud) = this install makes no outbound calls to OtoDock; hosted toggles show
  // "disabled (air-gapped)". `relay_available` = connected AND a relay base is
  // configured server-side (toggles show "activates when live" until available).
  air_gapped: boolean
  relay_available: boolean
  cloud: boolean
}

export interface LoginResult {
  user?: User
  requires_2fa?: boolean
  totp_session_token?: string
  // Which second factors the account can complete step 2 with ('passkey' / 'totp')
  second_factors?: string[]
}

// --- Auth config (public, no auth needed) ---

export async function fetchAuthConfig(): Promise<AuthConfig> {
  const res = await fetch('/auth/config')
  if (!res.ok) {
    return {
      oidc_enabled: false, oidc_provider_name: 'SSO',
      turnstile_site_key: '', setup_required: false,
      auth_provider_bypass: false, smtp_configured: false,
      email_links_available: false,
      password_min_score: 3, password_min_length: 8,
      passkeys_enabled: false, passkey_login_mode: 'passwordless',
      passkey_rp_host: '',
      air_gapped: true, relay_available: false, cloud: false,
    }
  }
  return res.json()
}

// --- OIDC flow (existing, refactored) ---

export async function startOidcLogin(mobile?: boolean): Promise<void> {
  const isNative = !!(window as any).Capacitor?.isNativePlatform?.()
  const url = isNative || mobile ? '/auth/oidc-url?mobile=true' : '/auth/oidc-url'
  const res = await fetch(url)
  if (!res.ok) throw new Error('OIDC not configured')
  const data = await res.json()

  if (isNative) {
    const android = (window as any).Android
    if (android?.openAuthBrowser) {
      android.openAuthBrowser(data.url)
      return
    }
  }
  window.location.href = data.url
}

/** Legacy startLogin — now handles bypass mode or delegates to OIDC */
export async function startLogin(): Promise<void> {
  const isNative = !!(window as any).Capacitor?.isNativePlatform?.()
  const loginUrl = isNative ? '/auth/login?mobile=true' : '/auth/login'
  const res = await fetch(loginUrl)
  if (!res.ok) throw new Error('Failed to initiate login')
  const data = await res.json()

  if (data.url) {
    // Bypass mode — direct OIDC redirect
    if (isNative) {
      const android = (window as any).Android
      if (android?.openAuthBrowser) {
        android.openAuthBrowser(data.url)
        return
      }
    }
    window.location.href = data.url
  }
  // If login_page: true — frontend handles showing the login page
}

export async function handleCallback(code: string, state: string): Promise<User> {
  const res = await fetch('/auth/callback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ code, state }),
  })
  if (res.status === 403) throw new Error('ACCESS_DENIED')
  if (res.status === 402) throw new Error('USER_LIMIT_REACHED')
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(detail.detail || 'Callback failed')
  }
  const data = await res.json()
  return data.user
}

// --- Local login ---

export async function localLogin(
  email: string, password: string, turnstileToken?: string
): Promise<LoginResult> {
  const res = await fetch('/auth/login/local', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ email, password, turnstile_token: turnstileToken }),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(detail.detail || 'Login failed')
  }
  return res.json()
}

export async function verify2FA(
  totpSessionToken: string, code: string
): Promise<User> {
  const res = await fetch('/auth/login/2fa', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ totp_session_token: totpSessionToken, code }),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(detail.detail || '2FA verification failed')
  }
  const data = await res.json()
  return data.user
}

// --- Setup wizard ---

export async function setupFirstUser(
  email: string, password: string, displayName: string
): Promise<{ user: User; dashboardUrlCaptured: boolean }> {
  const res = await fetch('/auth/setup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
      email, password, display_name: displayName,
      // First-run capture: the true origin the admin browses from becomes
      // DASHBOARD_PUBLIC_URL when the operator never set one (preview works
      // out of the box). The server never overwrites an explicit value.
      origin: window.location.origin,
    }),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(detail.detail || 'Setup failed')
  }
  const data = await res.json()
  return { user: data.user, dashboardUrlCaptured: !!data.dashboard_url_captured }
}

// --- Session ---

export async function fetchCurrentUser(): Promise<User | null> {
  try {
    const res = await fetch('/auth/me', { credentials: 'same-origin' })
    if (!res.ok) return null
    const data = await res.json()
    return data.user
  } catch {
    return null
  }
}

export async function logout(): Promise<void> {
  const res = await fetch('/auth/logout', {
    method: 'POST',
    credentials: 'same-origin',
  })
  const data = await res.json().catch(() => ({}))
  // OIDC users get redirected to provider logout, local users to login page
  window.location.href = data.logout_url || '/'
}

// --- Password reset ---

export async function forgotPassword(email: string): Promise<string> {
  const res = await fetch('/auth/forgot-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  })
  const data = await res.json().catch(() => ({}))
  return data.message || 'If your email is registered, you will receive a reset link.'
}

export async function resetPassword(token: string, newPassword: string): Promise<void> {
  const res = await fetch('/auth/reset-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, new_password: newPassword }),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(detail.detail || 'Password reset failed')
  }
}

// --- Invite acceptance (tokenized admin invite link) ---

export async function acceptInvite(token: string, newPassword: string): Promise<{ email?: string }> {
  const res = await fetch('/auth/accept-invite', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, new_password: newPassword }),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(detail.detail || 'Failed to activate account')
  }
  return res.json()
}

// --- API fetch wrapper ---

export async function apiFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const isFormData = options.body instanceof FormData
  const headers: Record<string, string> = isFormData
    ? { ...options.headers as Record<string, string> }
    : { 'Content-Type': 'application/json', ...options.headers as Record<string, string> }

  const res = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers,
  })
  if (res.status === 401) {
    window.location.href = '/'
  }
  return res
}
