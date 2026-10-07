// A login page left open across an upgrade keeps serving the old bundle
// unless it watches the build like the signed-in pages do: the signed-out
// page arms the watch on its own, and disarms it while a reload would lose
// what the person is doing (a typed password, the second-factor step, a
// sign-in request in flight).

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  useBuildWatch: vi.fn(), localLogin: vi.fn(), passkeyLogin: vi.fn(), passkeySupported: false,
}))

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ setUser: vi.fn() }) }))
vi.mock('@/api/auth', () => ({
  localLogin: h.localLogin, verify2FA: vi.fn(), startOidcLogin: vi.fn(), forgotPassword: vi.fn(),
}))
vi.mock('@/api/webauthn', () => ({
  passkeyLogin: h.passkeyLogin, passkeySecondFactor: vi.fn(), passkeySupported: () => h.passkeySupported,
  startNativeHandoff: vi.fn(),
}))
vi.mock('@/components/TurnstileWidget', () => ({ TurnstileWidget: () => null }))
vi.mock('@/hooks/useBuildWatch', () => ({ useBuildWatch: h.useBuildWatch }))

import LoginPage from '@/pages/LoginPage'

function renderLogin(passkeys = false) {
  return render(
    <MemoryRouter>
      <LoginPage authConfig={{
        oidc_enabled: false, oidc_provider_name: '', passkeys_enabled: passkeys,
        passkey_login_mode: 'passwordless', email_links_available: false, turnstile_site_key: '',
        passkey_rp_host: '',
      } as any} />
    </MemoryRouter>,
  )
}

const armed = () => h.useBuildWatch.mock.lastCall?.[0]
const passwordInput = () => document.querySelector('input[type="password"]') as HTMLInputElement

beforeEach(() => {
  h.useBuildWatch.mockClear()
  h.localLogin.mockReset()
  h.passkeyLogin.mockReset()
  h.passkeySupported = false
})

describe('LoginPage build watch', () => {
  it('arms the build watch while signed out', () => {
    renderLogin()
    expect(h.useBuildWatch).toHaveBeenCalledWith(true)
    expect(armed()).toBe(true)
  })

  it('a typed, unsent password disarms it until the field is empty again', () => {
    renderLogin()
    fireEvent.change(passwordInput(), { target: { value: 'hunter2' } })
    expect(armed()).toBe(false)
    fireEvent.change(passwordInput(), { target: { value: '' } })
    expect(armed()).toBe(true)
  })

  it('the second-factor step disarms it, and a cancelled step with an empty password re-arms it', async () => {
    let finish!: (v: unknown) => void
    h.localLogin.mockReturnValue(new Promise((res) => { finish = res }))
    renderLogin()
    fireEvent.change(document.querySelector('input[type="email"]')!, { target: { value: 'a@example.com' } })
    fireEvent.change(passwordInput(), { target: { value: 'hunter2' } })
    fireEvent.submit(passwordInput().form!)
    // The sign-in request in flight holds it too.
    expect(armed()).toBe(false)
    finish({ requires_2fa: true, totp_session_token: 'step-token', second_factors: ['totp'] })
    await screen.findByText("Verify it's you")
    expect(armed()).toBe(false)
    fireEvent.click(screen.getByText('Back to sign in'))
    await waitFor(() => expect(passwordInput()).toBeTruthy())
    expect(armed()).toBe(false)
    fireEvent.change(passwordInput(), { target: { value: '' } })
    expect(armed()).toBe(true)
  })

  it('an in-page passkey ceremony disarms it with the password empty, and its end re-arms it', async () => {
    h.passkeySupported = true
    let fail!: (e: unknown) => void
    h.passkeyLogin.mockReturnValue(new Promise((_res, rej) => { fail = rej }))
    renderLogin(true)
    expect(passwordInput().value).toBe('')
    expect(armed()).toBe(true)
    fireEvent.click(screen.getByText('Sign in with a passkey'))
    expect(armed()).toBe(false)
    fail(Object.assign(new Error('cancelled'), { name: 'NotAllowedError' }))
    await waitFor(() => expect(armed()).toBe(true))
  })
})
