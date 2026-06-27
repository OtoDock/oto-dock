// SSO-fronted login surface: when OIDC is enabled the SSO button must be the
// FIRST action and the local passkey button demoted + explicitly labeled as a
// local-account sign-in. Regression for the live-observed trap: an operator
// arriving mid-Authentik flow clicked the familiar top passkey button and got
// signed into the local account that owned the host's only passkey.

import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ setUser: vi.fn() }),
}))
vi.mock('@/api/auth', () => ({
  localLogin: vi.fn(),
  verify2FA: vi.fn(),
  startOidcLogin: vi.fn(),
  forgotPassword: vi.fn(),
}))
vi.mock('@/api/webauthn', () => ({
  passkeyLogin: vi.fn(),
  passkeySecondFactor: vi.fn(),
  passkeySupported: () => true,
}))
vi.mock('@/components/TurnstileWidget', () => ({
  TurnstileWidget: () => null,
}))

import LoginPage from '@/pages/LoginPage'

function cfg(over: Record<string, unknown> = {}) {
  return {
    oidc_enabled: false,
    oidc_provider_name: 'Authentik',
    passkeys_enabled: true,
    passkey_login_mode: 'passwordless',
    email_links_available: false,
    turnstile_site_key: '',
    ...over,
  } as any
}

function renderPage(config: any) {
  return render(
    <MemoryRouter>
      <LoginPage authConfig={config} />
    </MemoryRouter>,
  )
}

describe('LoginPage SSO-first ordering', () => {
  it('renders SSO before the passkey button and labels it local-account', () => {
    renderPage(cfg({ oidc_enabled: true }))
    const sso = screen.getByRole('button', { name: /continue with authentik/i })
    const passkey = screen.getByRole('button', { name: /local-account passkey/i })
    // SSO must precede the passkey button in document order.
    expect(
      sso.compareDocumentPosition(passkey) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    expect(screen.getByText(/or use a local account/i)).toBeTruthy()
  })

  it('keeps passkey-first with the plain label on local-only installs', () => {
    renderPage(cfg())
    expect(
      screen.getByRole('button', { name: /^sign in with a passkey$/i }),
    ).toBeTruthy()
    expect(screen.queryByRole('button', { name: /continue with/i })).toBeNull()
  })

  it('does not prime passkey autofill on the email field', () => {
    renderPage(cfg({ oidc_enabled: true }))
    const email = screen.getByPlaceholderText('you@example.com')
    expect(email.getAttribute('autocomplete')).toBe('email')
  })
})
