/**
 * useDuplexVoice.getLevels (Stage Mode Phase B): zeros when idle — no
 * session means no mic analyser and no player, and the halo must get a
 * well-formed answer, not a crash.
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async () => ({
    ok: true, json: async () => ({ ws_token: 'tok-1' }),
  })),
}))

import { useDuplexVoice } from '@/hooks/useDuplexVoice'

describe('useDuplexVoice.getLevels', () => {
  it('returns zeros while idle and keeps a stable identity', () => {
    const hook = renderHook(() => useDuplexVoice('chat-1'))
    const first = hook.result.current.getLevels
    expect(first()).toEqual({ mic: 0, out: 0 })

    hook.rerender()
    expect(hook.result.current.getLevels).toBe(first) // ref-safe for rAF polling
  })
})
