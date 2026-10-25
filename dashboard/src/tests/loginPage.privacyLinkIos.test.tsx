// The privacy link on the sign-in page with the real shell check, in the
// fallback when no native channel exists (an iOS shell before its port
// injects one): Capacitor's native platform alone shows the link, and it
// opens the policy through the system browser. A person signed out of an
// added server meets this page first.

import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({ opened: [] as string[] }))

vi.mock('@capacitor/core', () => ({ Capacitor: { isNativePlatform: () => true, getPlatform: () => 'ios' } }))
vi.mock('@/lib/openExternal', () => ({
  openExternalUrl: async (url: string) => { h.opened.push(url); return 'opened' },
}))
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

import LoginPage from '@/pages/LoginPage'
import { APP_PRIVACY_POLICY_URL } from '@/lib/nativeBridge'

describe('LoginPage privacy link in an iOS shell', () => {
  it('shows signed out and opens the policy', () => {
    expect((window as unknown as { OtoDockNative?: unknown }).OtoDockNative).toBeUndefined()
    render(
      <MemoryRouter>
        <LoginPage authConfig={{
          oidc_enabled: false, oidc_provider_name: '', passkeys_enabled: false,
          passkey_login_mode: 'second_factor', email_links_available: false, turnstile_site_key: '',
        } as never} />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: /privacy policy/i }))
    expect(h.opened).toEqual([APP_PRIVACY_POLICY_URL])
  })
})
