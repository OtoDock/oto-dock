/**
 * Presence tri-state over the dashboard WS.
 *
 * The backend routes end-of-turn alerts on three states, not two:
 * 'active' (visible + recent input) → `user_active`; 'away' (visible but
 * input-idle ~5 min — the toast would play to an empty chair, so FCM also
 * fires) → `user_idle {away:true}`; 'idle' (hidden tab — same-machine
 * multitasking, no buzz) → `user_idle {away:false}`. The dedupe must key on
 * the TRI-state: a boolean would swallow the away→hidden transition and the
 * backend would keep pushing for a tab the user just backgrounded.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act } from '@testing-library/react'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { FakeWebSocket } from './fakeWebSocket'
import { connectDashboardWs } from './dashboardWsHarness'

let visState: DocumentVisibilityState = 'visible'

function presenceMessages(ws: FakeWebSocket): Array<Record<string, unknown>> {
  return ws.sent
    .map((s) => JSON.parse(s))
    .filter((m) => m.type === 'user_active' || m.type === 'user_idle')
}

function last<T>(arr: T[]): T | undefined {
  return arr[arr.length - 1]
}

describe('useDashboardWs presence tri-state', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
    visState = 'visible'
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => visState,
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function connect() {
    return connectDashboardWs(() => ({}))
  }

  function setVisibility(v: DocumentVisibilityState) {
    visState = v
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
  }

  it('walks active → away → idle → active, deduped per tri-state', () => {
    const { ws } = connect()
    // onopen force-sends the real state (visible + not idle)
    expect(presenceMessages(ws)).toEqual([{ type: 'user_active' }])

    // 5 min without input while visible → away
    act(() => { vi.advanceTimersByTime(5 * 60 * 1000) })
    expect(last(presenceMessages(ws))).toEqual({ type: 'user_idle', away: true })

    // hiding the away tab must re-send (away→idle) even though "not active"
    // is unchanged — the backend stops treating it as an empty chair
    setVisibility('hidden')
    expect(last(presenceMessages(ws))).toEqual({ type: 'user_idle', away: false })

    // repeat visibility event → deduped, no new presence message
    const count = presenceMessages(ws).length
    setVisibility('hidden')
    expect(presenceMessages(ws).length).toBe(count)

    // returning to the tab counts as activity → active again
    setVisibility('visible')
    expect(last(presenceMessages(ws))).toEqual({ type: 'user_active' })
  })

  it('idle timer firing on a hidden tab stays deduped as plain idle', () => {
    const { ws } = connect()
    setVisibility('hidden')
    expect(last(presenceMessages(ws))).toEqual({ type: 'user_idle', away: false })
    const count = presenceMessages(ws).length
    // the 5-min timer firing while hidden computes the same 'idle' state
    act(() => { vi.advanceTimersByTime(5 * 60 * 1000) })
    expect(presenceMessages(ws).length).toBe(count)
  })
})
