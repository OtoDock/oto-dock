import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// ─── User Settings, Two-Factor Authentication: a passkey's Rename or Remove
//     opens its password box under that passkey, not under the whole list;
//     Escape and Cancel close it ───

vi.mock('@/api/webauthn', () => ({
  listPasskeys: vi.fn(async () => ({
    enabled: true,
    rp_host: '',
    passkeys: [
      { credential_id: 'k1', name: 'Laptop', created_at: '', last_used: '' },
      { credential_id: 'k2', name: 'Phone', created_at: '', last_used: '' },
      { credential_id: 'k3', name: 'Tablet', created_at: '', last_used: '' },
    ],
  })),
  passkeySupported: () => true,
  registerPasskey: vi.fn(),
  renamePasskey: vi.fn(),
  deletePasskey: vi.fn(),
}))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'member', totp_enabled: true }, setUser: vi.fn() }),
}))

import { TwoFactorSection } from '@/pages/UserSettings.general'

const follows = (a: Node, b: Node) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)

describe('TwoFactorSection: the passkey box opens under its passkey', () => {
  it('Rename on the second passkey opens between it and the third', async () => {
    render(<TwoFactorSection />)
    await screen.findByText('Phone')
    fireEvent.click(screen.getAllByRole('button', { name: 'Rename' })[1])
    const field = screen.getByPlaceholderText('Passkey name')
    expect(follows(screen.getByText('Phone'), field)).toBe(true)
    expect(follows(field, screen.getByText('Tablet'))).toBe(true)
    expect(follows(field, screen.getByText('Authenticator app'))).toBe(true)
  })

  it('Escape inside the box closes it', async () => {
    render(<TwoFactorSection />)
    await screen.findByText('Laptop')
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0])
    const password = screen.getByPlaceholderText('Password')
    fireEvent.keyDown(password, { key: 'Escape' })
    expect(screen.queryByPlaceholderText('Password')).toBeNull()
  })

  it('Cancel closes it', async () => {
    render(<TwoFactorSection />)
    await screen.findByText('Laptop')
    fireEvent.click(screen.getAllByRole('button', { name: 'Rename' })[0])
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByPlaceholderText('Passkey name')).toBeNull()
  })
})
