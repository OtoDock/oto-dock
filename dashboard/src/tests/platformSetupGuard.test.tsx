// The setup-required gate replaces the whole app shell (no account menu), so
// it must carry its own sign-out — a gated user previously had no way to log
// out at all.

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const h = vi.hoisted(() => ({ logout: vi.fn() }))

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { sub: 'u1', role: 'member', platform_configured: false },
    logout: h.logout,
  }),
}))

import PlatformSetupGuard from '@/components/PlatformSetupGuard'

describe('PlatformSetupGuard sign-out escape hatch', () => {
  it('shows the gate with a working Sign out button', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <PlatformSetupGuard />
      </MemoryRouter>,
    )
    expect(screen.getByText('Platform Setup Required')).toBeTruthy()
    const signOut = screen.getByRole('button', { name: /sign out/i })
    fireEvent.click(signOut)
    expect(h.logout).toHaveBeenCalledOnce()
  })

  it('lets a gated member into every User Settings section, where the gate sends them', () => {
    for (const path of ['/user-settings', '/user-settings/ai-engines', '/user-settings/general']) {
      const r = render(
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route element={<PlatformSetupGuard />}>
              <Route path="*" element={<p>settings page</p>} />
            </Route>
          </Routes>
        </MemoryRouter>,
      )
      expect(screen.getByText('settings page')).toBeTruthy()
      expect(screen.queryByText('Platform Setup Required')).toBeNull()
      r.unmount()
    }
  })

  it('still blocks a page that only starts like User Settings', () => {
    render(
      <MemoryRouter initialEntries={['/user-settingsx']}>
        <PlatformSetupGuard />
      </MemoryRouter>,
    )
    expect(screen.getByText('Platform Setup Required')).toBeTruthy()
  })
})
