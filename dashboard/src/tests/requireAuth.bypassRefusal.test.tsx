/**
 * Bypass mode (`auth_provider_bypass` + OIDC) sends a signed-out tab straight
 * to the provider. A start the server refuses (the per-address SSO bound's
 * 429, proxy API.md) used to land only in the console while the page said
 * "Redirecting to login..." for ever; the person now reads the server's
 * reason and its wait, and the page asks no second time on its own.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

vi.mock('@/hooks/useFcmPush', () => ({ useFcmPush: () => {} }))
vi.mock('@/hooks/useWakeWord', () => ({ useWakeWord: () => {} }))
vi.mock('@/hooks/useBuildWatch', () => ({ useBuildWatch: () => {} }))
vi.mock('@/hooks/useOpenAppToast', () => ({ useOpenAppToast: () => null }))
vi.mock('@/components/WakeDiagBadge', () => ({ default: () => null }))

import { AuthProvider } from '@/contexts/AuthContext'
import RequireAuth from '@/components/RequireAuth'

const DETAIL = 'Too many sign-ins started; try again in a few minutes.'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('RequireAuth in bypass mode', () => {
  it('shows a refused SSO start with its wait instead of redirecting for ever', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === '/auth/config') {
        return {
          ok: true, status: 200,
          json: async () => ({ auth_provider_bypass: true, oidc_enabled: true, setup_required: false }),
        } as unknown as Response
      }
      if (url === '/auth/me') {
        return { ok: false, status: 401, json: async () => ({}) } as unknown as Response
      }
      return {
        ok: false, status: 429,
        headers: new Headers({ 'Retry-After': '90' }),
        json: async () => ({ detail: DETAIL }),
      } as unknown as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <AuthProvider>
        <MemoryRouter>
          <Routes>
            <Route element={<RequireAuth />}>
              <Route path="/" element={<div>home</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AuthProvider>,
    )
    const shown = await screen.findByText(new RegExp(DETAIL.replace(/[.;]/g, '\\$&')))
    expect(shown.textContent).toContain('(retry in 2 minutes)')
    expect(screen.queryByText(/Redirecting to login/)).toBeNull()
    expect(screen.getByRole('button', { name: /try again/i })).toBeTruthy()
    const starts = fetchMock.mock.calls.filter(([u]) => String(u).startsWith('/auth/login'))
    expect(starts.length).toBe(1)
  })
})
