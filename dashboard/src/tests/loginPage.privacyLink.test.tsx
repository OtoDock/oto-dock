// Inside the app shell the login page carries the privacy-policy link the
// stores require on every screen a person meets before signing in; the web
// dashboard shows none.

import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const bridge = vi.hoisted(() => ({ inShell: false, opened: 0 }))

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
  startNativeHandoff: vi.fn(),
}))
vi.mock('@/components/TurnstileWidget', () => ({
  TurnstileWidget: () => null,
}))
vi.mock('@/lib/nativeBridge', () => ({
  callNative: vi.fn(() => false),
  hasNativeBridge: () => false,
  inAppShell: () => bridge.inShell,
  openNativeBrowser: vi.fn(),
  openPrivacyPolicy: () => { bridge.opened += 1 },
}))

import LoginPage from '@/pages/LoginPage'

const config = {
  oidc_enabled: false,
  oidc_provider_name: '',
  passkeys_enabled: true,
  passkey_login_mode: 'passwordless',
  email_links_available: false,
  turnstile_site_key: '',
} as any

function renderPage() {
  return render(
    <MemoryRouter>
      <LoginPage authConfig={config} />
    </MemoryRouter>,
  )
}

describe('LoginPage privacy link', () => {
  it('shows the link inside the app shell and opens the policy', () => {
    bridge.inShell = true
    renderPage()
    const link = screen.getByRole('button', { name: /privacy policy/i })
    fireEvent.click(link)
    expect(bridge.opened).toBe(1)
  })

  it('keeps the link on the password reset screen', () => {
    bridge.inShell = true
    render(
      <MemoryRouter>
        <LoginPage authConfig={{ ...config, email_links_available: true }} />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: /forgot password/i }))
    expect(screen.getByText('Reset password')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /privacy policy/i })).toBeInTheDocument()
  })

  it('shows no link in the web dashboard', () => {
    bridge.inShell = false
    renderPage()
    expect(screen.queryByRole('button', { name: /privacy policy/i })).toBeNull()
  })
})
