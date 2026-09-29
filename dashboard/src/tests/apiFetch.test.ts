import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { apiFetch } from '@/api/auth'

// ─── A 401 is a lost session only when /auth/me agrees. A share link's
//     password confirm and a passkey confirm answer 401 for a wrong password
//     or an expired confirmation; sending the tab to `/` there dropped the
//     form and the reason the caller was about to show. ─────────────────────

const originalLocation = window.location

function stubFetch(me: number | 'network') {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    calls.push(path)
    if (path === '/auth/me') {
      if (me === 'network') throw new TypeError('network down')
      return new Response('{}', { status: me })
    }
    return new Response(JSON.stringify({ detail: 'Password is incorrect' }), { status: 401 })
  }))
  return calls
}

beforeEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: { href: '/apps/a1' } })
})

afterEach(() => {
  vi.unstubAllGlobals()
  Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
})

describe('apiFetch on a 401', () => {
  it('stays on the page when the session is still valid, and hands the answer back', async () => {
    const calls = stubFetch(200)
    const res = await apiFetch('/v1/shares', { method: 'POST', body: '{}' })
    expect(res.status).toBe(401)
    expect(calls).toEqual(['/v1/shares', '/auth/me'])
    expect(window.location.href).toBe('/apps/a1')
  })

  it('goes to the sign-in page when the session is gone', async () => {
    stubFetch(401)
    await apiFetch('/v1/agents')
    expect(window.location.href).toBe('/')
  })

  it('goes to the sign-in page when the session cannot be checked', async () => {
    stubFetch('network')
    await apiFetch('/v1/agents')
    expect(window.location.href).toBe('/')
  })
})

// ─── A forced password change or 2FA enrolment that starts while a page is
//     open (an admin reset, require_2fa switched on) answers 403 with
//     X-Auth-Gate on every route but the exempt ones; the tab goes to the
//     screen the gate names instead of showing a page of failed calls. ─────

function stubGated(gate: string | null, status = 403) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{"detail":"held"}', {
    status,
    headers: gate ? { 'X-Auth-Gate': gate } : {},
  })))
}

describe('apiFetch on a gated 403', () => {
  it('sends the tab to the change-password screen', async () => {
    stubGated('must_change_password')
    Object.defineProperty(window, 'location', {
      configurable: true, value: { href: '/apps/a1', pathname: '/apps/a1' },
    })
    await apiFetch('/v1/agents')
    expect(window.location.href).toBe('/change-password')
  })

  it('sends the tab to the 2FA enrolment screen', async () => {
    stubGated('must_enroll_2fa')
    Object.defineProperty(window, 'location', {
      configurable: true, value: { href: '/apps/a1', pathname: '/apps/a1' },
    })
    await apiFetch('/v1/agents')
    expect(window.location.href).toBe('/setup-2fa')
  })

  it('stays put when already on the forced screen', async () => {
    stubGated('must_change_password')
    Object.defineProperty(window, 'location', {
      configurable: true, value: { href: '/change-password', pathname: '/change-password' },
    })
    await apiFetch('/v1/users/me/audio-prefs')
    expect(window.location.href).toBe('/change-password')
  })

  it('leaves an ordinary 403 to the caller', async () => {
    stubGated(null)
    Object.defineProperty(window, 'location', {
      configurable: true, value: { href: '/apps/a1', pathname: '/apps/a1' },
    })
    const res = await apiFetch('/v1/agents/x')
    expect(res.status).toBe(403)
    expect(window.location.href).toBe('/apps/a1')
  })
})
