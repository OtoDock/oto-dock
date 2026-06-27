// The setup-required gate replaces the whole app shell (no account menu), so
// it must carry its own sign-out — a gated user previously had no way to log
// out at all.

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

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
})
