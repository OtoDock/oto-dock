import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  useAgentPrefsStore,
  hydrateLastInteractiveFromServer,
  __resetUiPrefsRoamingForTests,
} from '@/store/agentPrefsStore'
import * as authApi from '@/api/auth'

// ─── agentPrefsStore server roaming (Item 5d-3): hydration merge semantics +
//     the debounced full-map write-through on setLastInteractive ─────────────

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

beforeEach(() => {
  __resetUiPrefsRoamingForTests()
  useAgentPrefsStore.getState().reset()
  fetchSpy.mockResolvedValue({ ok: true, json: async () => ({}) } as Response)
})
afterEach(() => {
  fetchSpy.mockReset()
  vi.useRealTimers()
})

describe('hydrateLastInteractiveFromServer', () => {
  it('server values fill/overwrite the local map; local-only keys are kept', () => {
    useAgentPrefsStore.setState({ lastInteractive: { a: '-p', c: 'interactive' } })
    hydrateLastInteractiveFromServer({ a: 'interactive', b: '-p' })
    expect(useAgentPrefsStore.getState().lastInteractive).toEqual({
      a: 'interactive', b: '-p', c: 'interactive',
    })
  })

  it('takes only explicit modes from the free-form bag', () => {
    hydrateLastInteractiveFromServer({ a: 'interactive', junk: 42, other: 'bogus' })
    expect(useAgentPrefsStore.getState().lastInteractive).toEqual({ a: 'interactive' })
  })

  it('hydrates ONCE per session load (a refetch must not clobber later toggles)', () => {
    hydrateLastInteractiveFromServer({ a: 'interactive' })
    useAgentPrefsStore.setState({ lastInteractive: { a: '-p' } })
    hydrateLastInteractiveFromServer({ a: 'interactive', b: '-p' })
    expect(useAgentPrefsStore.getState().lastInteractive).toEqual({ a: '-p' })
  })

  it('never writes back to the server (no PUT loop)', () => {
    vi.useFakeTimers()
    hydrateLastInteractiveFromServer({ a: 'interactive' })
    vi.advanceTimersByTime(5000)
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})

describe('setLastInteractive write-through', () => {
  it('debounces rapid toggles into ONE PUT carrying the FULL map', () => {
    vi.useFakeTimers()
    useAgentPrefsStore.getState().setLastInteractive('a', 'interactive')
    useAgentPrefsStore.getState().setLastInteractive('b', '-p')
    expect(fetchSpy).not.toHaveBeenCalled()  // still inside the debounce window
    vi.advanceTimersByTime(1100)
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    const [path, options] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/v1/users/me/ui-prefs')
    expect(options.method).toBe('PUT')
    // Full map, not just the changed key — the server merge is top-level
    // shallow, so a single-agent payload would wipe the other agents' modes.
    expect(JSON.parse(options.body as string)).toEqual({
      last_execution_mode: { a: 'interactive', b: '-p' },
    })
  })

  it('local state updates immediately even while the PUT is pending', () => {
    vi.useFakeTimers()
    useAgentPrefsStore.getState().setLastInteractive('a', '-p')
    expect(useAgentPrefsStore.getState().lastInteractive).toEqual({ a: '-p' })
  })
})
