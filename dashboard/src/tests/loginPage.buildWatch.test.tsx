// A login page left open across an upgrade keeps serving the old bundle
// unless it watches the build like the signed-in pages do: the signed-out
// page arms the watch on its own.

import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({ useBuildWatch: vi.fn() }))

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ setUser: vi.fn() }) }))
vi.mock('@/api/auth', () => ({
  localLogin: vi.fn(), verify2FA: vi.fn(), startOidcLogin: vi.fn(), forgotPassword: vi.fn(),
}))
vi.mock('@/api/webauthn', () => ({
  passkeyLogin: vi.fn(), passkeySecondFactor: vi.fn(), passkeySupported: () => false,
  startNativeHandoff: vi.fn(),
}))
vi.mock('@/components/TurnstileWidget', () => ({ TurnstileWidget: () => null }))
vi.mock('@/hooks/useBuildWatch', () => ({ useBuildWatch: h.useBuildWatch }))

import LoginPage from '@/pages/LoginPage'

describe('LoginPage build watch', () => {
  it('arms the build watch while signed out', () => {
    render(
      <MemoryRouter>
        <LoginPage authConfig={{
          oidc_enabled: false, oidc_provider_name: '', passkeys_enabled: false,
          passkey_login_mode: 'passwordless', email_links_available: false, turnstile_site_key: '',
        } as any} />
      </MemoryRouter>,
    )
    expect(h.useBuildWatch).toHaveBeenCalledWith(true)
  })
})
