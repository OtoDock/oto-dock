import { StrictMode } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

// The callback page exchanges the code; the exchange and the auth context
// are stubbed, the routing and the storage are the subject.
const exchange = vi.hoisted(() => ({ handle: vi.fn(), native: vi.fn() }))
vi.mock('@/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/auth')>()),
  handleCallback: exchange.handle,
}))
vi.mock('@/api/webauthn', () => ({ NATIVE_HANDOFF_STATE: 'passkey-handoff', exchangeNativeToken: exchange.native }))
const auth = vi.hoisted(() => ({ setUser: vi.fn(), user: { sub: 'u-me' } as { sub: string } | null, loading: false }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => auth }))

import AuthCallback from '@/pages/AuthCallback'
import { savePending } from '@/lib/shareConfirm'
import { installFakeNativeChannel } from './fixtures/nativeChannel'

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

function tree(search: string) {
  return (
    <MemoryRouter initialEntries={[`/auth/callback${search}`]}>
      <Routes>
        <Route path="/auth/callback" element={<AuthCallback />} />
        <Route path="/apps/:id" element={<LocationProbe />} />
        <Route path="/" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>
  )
}

function renderAt(search: string) {
  return render(tree(search))
}

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)) })

beforeEach(() => {
  exchange.handle.mockReset()
  exchange.native.mockReset()
  auth.setUser.mockClear()
  auth.user = { sub: 'u-me' }
  auth.loading = false
  localStorage.clear()
})

describe('AuthCallback', () => {
  it('a passkey handoff link outside the app exchanges nothing and says why', async () => {
    renderAt('?code=tok&state=passkey-handoff')
    await flush()
    expect(exchange.native).not.toHaveBeenCalled()
    expect(screen.getByText(/only works in the OtoDock app/i)).toBeTruthy()
  })

  it('the app exchanges its passkey handoff', async () => {
    ;(window as any).Capacitor = { isNativePlatform: () => true }
    try {
      exchange.native.mockResolvedValueOnce({ sub: 'u-me' })
      renderAt('?code=tok&state=passkey-handoff')
      await flush()
      expect(exchange.native).toHaveBeenCalledWith('tok')
    } finally {
      delete (window as any).Capacitor
    }
  })

  it('the app persists its new session cookie through the channel after the exchange', async () => {
    ;(window as any).Capacitor = { isNativePlatform: () => true }
    const ch = installFakeNativeChannel()
    try {
      exchange.native.mockResolvedValueOnce({ sub: 'u-me' })
      renderAt('?code=tok&state=passkey-handoff')
      await flush()
      expect(ch.methods()).toEqual(['flushCookies'])
    } finally {
      delete (window as any).Capacitor
      ch.uninstall()
    }
  })

  it('a confirm answer files the token under the account and goes back to the share', async () => {
    exchange.handle.mockResolvedValueOnce({ kind: 'confirm', token: 'oc-9', return_to: '/apps/app-1?share=1&tab=link' })
    renderAt('?code=c&state=s')
    await flush()
    expect(screen.getByTestId('loc').textContent).toBe('/apps/app-1?share=1&tab=link')
    expect(JSON.parse(localStorage.getItem('otodock-confirm:u-me') || '{}').token).toBe('oc-9')
    expect(auth.setUser).not.toHaveBeenCalled()
  })

  it('a refused confirm goes back to the share with the reason, never to the failed-login page', async () => {
    savePending('u-me', {
      app_id: 'app-1', target_kind: 'app', op: 'create', tab: 'link', return_to: '/apps/app-1?share=1&tab=link',
      fields: { public: false, expiry: '30d', buttons: false, include_tools: false },
    })
    const err = new Error('ACCESS_DENIED') as Error & { detail?: string }
    err.detail = 'The login was not fresh'
    exchange.handle.mockRejectedValueOnce(err)
    renderAt('?code=c&state=s')
    await flush()
    expect(screen.getByTestId('loc').textContent).toBe('/apps/app-1?share=1&tab=link&confirm_error=The%20login%20was%20not%20fresh')
    expect(screen.queryByText('Authentication Failed')).toBeNull()
  })

  // The provider sent the browser back with no code and no state (its
  // application "launches" at the callback after a login — Authentik's
  // default, seen on the VM pass 2026-09-18): during a share confirm the
  // share gets the reason; otherwise the home, never an error page.
  it('an answerless callback goes back to a pending share with the cause, or home', async () => {
    savePending('u-me', {
      app_id: 'app-1', target_kind: 'app', op: 'create', tab: 'link', return_to: '/apps/app-1?share=1&tab=link',
      fields: { public: false, expiry: '30d', buttons: false, include_tools: false },
    })
    const { unmount } = renderAt('')
    await flush()
    const loc = screen.getByTestId('loc').textContent || ''
    expect(loc.startsWith('/apps/app-1?share=1&tab=link&confirm_error=')).toBe(true)
    expect(decodeURIComponent(loc)).toContain('"Launch URL" should point at this dashboard')
    expect(exchange.handle).not.toHaveBeenCalled()
    expect(screen.queryByText('Authentication Failed')).toBeNull()
    unmount()
    // Nothing pending (the popover consumed it, or there never was one).
    localStorage.clear()
    renderAt('')
    await flush()
    expect(screen.getByTestId('loc').textContent).toBe('/')
    expect(screen.queryByText('Missing code or state parameter')).toBeNull()
  })

  it('a login posts the code once, although the account arriving re-runs the effect', async () => {
    // The code is one-shot: a second POST with it gets "invalid or expired
    // state" and the page said "Authentication Failed" to a person who was,
    // in fact, signed in (found live on T1 with the mock provider).
    auth.user = null
    exchange.handle.mockResolvedValue({ kind: 'login', user: { sub: 'u-new', role: 'member' } })
    const { rerender } = renderAt('?code=c&state=s')
    await flush()
    expect(exchange.handle).toHaveBeenCalledTimes(1)
    auth.user = { sub: 'u-new' }
    rerender(tree('?code=c&state=s'))
    await flush()
    expect(exchange.handle).toHaveBeenCalledTimes(1)
    await act(async () => { await new Promise((r) => setTimeout(r, 600)) })
    expect(screen.getByTestId('loc').textContent).toBe('/')
    expect(screen.queryByText('Authentication Failed')).toBeNull()
  })

  it('a login answer still sets the user; a failure with nothing pending shows the page', async () => {
    exchange.handle.mockResolvedValueOnce({ kind: 'login', user: { sub: 'u-me', role: 'member' } })
    const { unmount } = renderAt('?code=c&state=s')
    await flush()
    expect(auth.setUser).toHaveBeenCalledWith({ sub: 'u-me', role: 'member' })
    unmount()
    exchange.handle.mockRejectedValueOnce(new Error('ACCESS_DENIED'))
    renderAt('?code=c&state=s')
    await flush()
    expect(screen.getByText('Authentication Failed')).toBeTruthy()
  })

  it('a login under StrictMode still lands (the remount revives the page)', async () => {
    exchange.handle.mockResolvedValueOnce({ kind: 'login', user: { sub: 'u-me', role: 'member' } })
    render(<StrictMode>{tree('?code=c&state=s')}</StrictMode>)
    await flush()
    expect(exchange.handle).toHaveBeenCalledTimes(1)
    expect(auth.setUser).toHaveBeenCalledWith({ sub: 'u-me', role: 'member' })
  })
})
