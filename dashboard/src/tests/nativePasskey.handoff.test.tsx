/**
 * The app's passkey sign-in is bound to the webview that starts it: the
 * login page asks for a nonce, persists the
 * webview's cookies, and opens the system browser with the nonce; the
 * system-browser page passes it to the verify, and refuses to run without it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const wa = vi.hoisted(() => ({
  start: vi.fn(), login: vi.fn(),
}))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ setUser: vi.fn() }) }))
vi.mock('@/api/auth', () => ({
  localLogin: vi.fn(), verify2FA: vi.fn(), startOidcLogin: vi.fn(), forgotPassword: vi.fn(),
}))
vi.mock('@/api/webauthn', () => ({
  NATIVE_HANDOFF_STATE: 'passkey-handoff',
  passkeyLogin: vi.fn(),
  passkeySecondFactor: vi.fn(),
  passkeySupported: () => true,
  startNativeHandoff: wa.start,
  nativePasskeyLogin: wa.login,
}))
vi.mock('@/components/TurnstileWidget', () => ({ TurnstileWidget: () => null }))

import LoginPage from '@/pages/LoginPage'
import NativePasskey from '@/pages/NativePasskey'
import { installFakeNativeChannel } from './fixtures/nativeChannel'

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)) })

const cfg = {
  oidc_enabled: false, oidc_provider_name: '', passkeys_enabled: true,
  passkey_login_mode: 'passwordless', email_links_available: false, turnstile_site_key: '',
} as any

describe('the app login page (native)', () => {
  const calls: string[] = []
  beforeEach(() => {
    calls.length = 0
    wa.start.mockReset()
    ;(window as any).Capacitor = { isNativePlatform: () => true }
    // The app's origin-scoped channel; every post joins the same log as the start.
    installFakeNativeChannel({ openAuthBrowser: true })
    const channel = (window as any).OtoDockNative
    const post = channel.postMessage.bind(channel)
    channel.postMessage = (message: string) => {
      const head = JSON.parse(message)
      calls.push(head.m === 'openAuthBrowser' ? `open ${head.a[0]} ${head.a[1]}` : head.m)
      post(message)
    }
  })
  afterEach(() => {
    delete (window as any).Capacitor
    delete (window as any).OtoDockNative
  })

  it('starts the handoff, persists the cookie, then opens the page with the nonce', async () => {
    wa.start.mockImplementation(async () => { calls.push('start'); return 'nonce-1' })
    render(<MemoryRouter><LoginPage authConfig={cfg} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /sign in with a passkey/i }))
    await flush()
    expect(calls).toEqual(['start', 'flushCookies', `open ${window.location.origin}/native-passkey?h=nonce-1 auth`])
  })

  it('opens nothing when the start fails, and shows why', async () => {
    wa.start.mockRejectedValueOnce(new Error('Passkeys require an HTTPS public dashboard URL'))
    render(<MemoryRouter><LoginPage authConfig={cfg} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /sign in with a passkey/i }))
    await flush()
    expect(calls).toEqual([])
    expect(screen.getByText(/HTTPS public dashboard URL/)).toBeTruthy()
  })

  it('an app without the channel shows no passkey button', () => {
    delete (window as any).OtoDockNative
    render(<MemoryRouter><LoginPage authConfig={cfg} /></MemoryRouter>)
    expect(screen.queryByRole('button', { name: /sign in with a passkey/i })).toBeNull()
  })
})

describe('the system-browser page', () => {
  beforeEach(() => wa.login.mockReset())

  it('passes the nonce to the verify', async () => {
    wa.login.mockResolvedValueOnce('tok')
    render(<MemoryRouter initialEntries={['/native-passkey?h=nonce-2']}><NativePasskey /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /use your passkey/i }))
    await flush()
    expect(wa.login).toHaveBeenCalledWith('nonce-2')
  })

  it('runs no ceremony without a nonce and says how to recover', () => {
    render(<MemoryRouter initialEntries={['/native-passkey']}><NativePasskey /></MemoryRouter>)
    expect(screen.queryByRole('button', { name: /use your passkey/i })).toBeNull()
    expect(screen.getByText(/close and reopen the OtoDock app/i)).toBeTruthy()
  })
})
