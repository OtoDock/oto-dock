import { describe, expect, it } from 'vitest'

import type { User } from '../api/auth'
import { FLAG_DEFAULTS, flagValue } from '../hooks/useFlag'

const user = (feature_flags?: User['feature_flags']) =>
  ({ sub: 's', email: 'e', name: 'n', role: 'member', feature_flags }) as unknown as User

describe('feature flags', () => {
  it('fail open and fall back to the shipped caps when /auth/me omits them', () => {
    expect(FLAG_DEFAULTS).toEqual({
      allow_user_paired_machines: true,
      remote_machines_available: true,
      interactive_terminal_enabled: true,
      upload_max_bytes: 1024 * 1024 * 1024,
      recover_bin_max_bytes: 100 * 1024 * 1024,
    })
    for (const u of [null, undefined, user(), user({})]) {
      expect(flagValue(u, 'remote_machines_available')).toBe(true)
      expect(flagValue(u, 'upload_max_bytes')).toBe(1024 * 1024 * 1024)
    }
  })

  it('reads a value the server sent, false and zero included', () => {
    const u = user({ remote_machines_available: false, upload_max_bytes: 0, recover_bin_max_bytes: 5 })
    expect(flagValue(u, 'remote_machines_available')).toBe(false)
    expect(flagValue(u, 'upload_max_bytes')).toBe(0)
    expect(flagValue(u, 'recover_bin_max_bytes')).toBe(5)
    expect(flagValue(u, 'interactive_terminal_enabled')).toBe(true)
  })
})
