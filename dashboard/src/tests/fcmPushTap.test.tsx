/**
 * A notification tap in the Android app (the switch is a question on the
 * origin-scoped channel): when the app answers that it
 * switched to the source install, the page does nothing; when it answers no,
 * or not at all, the page opens the notification's path in place.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import { installFakeNativeChannel } from './fixtures/nativeChannel'

type Tap = (n: { notification: { data: Record<string, unknown> } }) => unknown
const h = vi.hoisted(() => ({
  listeners: {} as Record<string, (arg: never) => unknown>,
  added: [] as string[],
  permission: 'granted',
  requested: 0,
  registered: 0,
  registerFails: false,
  platform: 'android',
}))
vi.mock('@capacitor/core', () => ({ Capacitor: { isNativePlatform: () => true, getPlatform: () => h.platform } }))
vi.mock('@capacitor/push-notifications', () => ({
  PushNotifications: {
    checkPermissions: async () => ({ receive: h.permission }),
    requestPermissions: async () => { h.requested++; return { receive: h.permission } },
    register: async () => {
      h.registered++
      if (h.registerFails) throw new Error('no Firebase')
    },
    addListener: async (name: string, fn: (arg: never) => unknown) => {
      h.listeners[name] = fn
      h.added.push(name)
      return { remove: () => {} }
    },
  },
}))
vi.mock('@/lib/device/storeReview', () => ({ noteStoreReviewMoment: vi.fn() }))
vi.mock('@/api/notifications', () => ({
  subscribePush: vi.fn(async () => {}),
  unsubscribePush: vi.fn(async () => true),
}))

import { subscribePush, unsubscribePush } from '@/api/notifications'
import { noteStoreReviewMoment } from '@/lib/device/storeReview'
import { PUSH_TOKEN_KEY, useFcmPush } from '@/hooks/useFcmPush'

function Probe({ navigate, enabled = true }: { navigate: (p: string) => void; enabled?: boolean }) {
  useFcmPush(navigate, enabled)
  return null
}

const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 0)) })
const TAP = { notification: { data: { install_id: 'p2', click_url: '/chat/pa/1' } } }

async function mountAndTap(navigate: (p: string) => void) {
  render(<Probe navigate={navigate} />)
  await settle()
  const tap = h.listeners.pushNotificationActionPerformed as unknown as Tap
  expect(tap).toBeTypeOf('function')
  return tap
}

beforeEach(() => {
  h.listeners = {}
  h.added = []
  h.permission = 'granted'
  h.requested = 0
  h.registered = 0
  h.registerFails = false
  h.platform = 'android'
  vi.mocked(subscribePush).mockClear()
  vi.mocked(noteStoreReviewMoment).mockClear()
  vi.mocked(unsubscribePush).mockReset()
  vi.mocked(unsubscribePush).mockResolvedValue(true)
  localStorage.removeItem(PUSH_TOKEN_KEY)
})
afterEach(() => {
  delete (window as unknown as { OtoDockNative?: unknown }).OtoDockNative
  vi.useRealTimers()
})

describe('a notification tap in the app', () => {
  it('asks the app to switch, and stays put when it did', async () => {
    const ch = installFakeNativeChannel({ switchToInstall: true })
    const navigate = vi.fn()
    const tap = await mountAndTap(navigate)
    await act(async () => { await tap(TAP) })
    expect(ch.posted).toMatchObject([{ m: 'switchToInstall', a: ['p2', '/chat/pa/1'] }])
    expect(navigate).not.toHaveBeenCalled()
  })

  it('opens the path in place when the app did not switch', async () => {
    installFakeNativeChannel({ switchToInstall: false })
    const navigate = vi.fn()
    const tap = await mountAndTap(navigate)
    await act(async () => { await tap(TAP) })
    expect(navigate).toHaveBeenCalledWith('/chat/pa/1')
  })

  it("a tap that opened a chat is the store rating's push moment; another page or a switch is not", async () => {
    const ch = installFakeNativeChannel({ switchToInstall: false })
    const navigate = vi.fn()
    const tap = await mountAndTap(navigate)
    await act(async () => { await tap(TAP) })
    expect(noteStoreReviewMoment).toHaveBeenCalledWith('push')
    vi.mocked(noteStoreReviewMoment).mockClear()
    await act(async () => { await tap({ notification: { data: { click_url: '/apps/board' } } }) })
    expect(navigate).toHaveBeenCalledWith('/apps/board')
    expect(noteStoreReviewMoment).not.toHaveBeenCalled()
    ch.uninstall()
    installFakeNativeChannel({ switchToInstall: true })
    await act(async () => { await tap(TAP) })
    expect(noteStoreReviewMoment).not.toHaveBeenCalled()
  })

  it('opens the path in place when the app does not answer', async () => {
    installFakeNativeChannel()
    const navigate = vi.fn()
    const tap = await mountAndTap(navigate)
    vi.useFakeTimers()
    const done = tap(TAP)
    await vi.advanceTimersByTimeAsync(3000)
    await done
    expect(navigate).toHaveBeenCalledWith('/chat/pa/1')
  })
})

describe('registering for push', () => {
  // The app's manifest keeps Firebase from starting a token on its own;
  // register() is what starts it, so it waits for the person's permission.
  it('registers once notifications are allowed', async () => {
    render(<Probe navigate={vi.fn()} />)
    await settle()
    expect(h.registered).toBe(1)
  })

  it('never registers when notifications are declined', async () => {
    h.permission = 'denied'
    render(<Probe navigate={vi.fn()} />)
    await settle()
    expect(h.registered).toBe(0)
    expect(h.listeners.pushNotificationActionPerformed).toBeUndefined()
  })

  // The relay adds Apple's block only to a token stored as an iPhone's.
  it.each([['ios', 'ios'], ['android', 'android']])('stores the token of %s as %s', async (platform, stored) => {
    h.platform = platform
    render(<Probe navigate={vi.fn()} />)
    await settle()
    const onToken = h.listeners.registration as unknown as (t: { value: string }) => Promise<void>
    await act(async () => { await onToken({ value: 'tok-1' }) })
    expect(subscribePush).toHaveBeenCalledWith(stored, 'tok-1')
  })
})

describe('notifications turned off in the phone\'s settings', () => {
  const comeBack = (event: 'visibility' | 'resume' = 'visibility') => act(async () => {
    if (event === 'visibility') document.dispatchEvent(new Event('visibilitychange'))
    else window.dispatchEvent(new Event('otodock:force-health-check'))
    await new Promise((r) => setTimeout(r, 0))
  })

  async function registeredWith(token: string) {
    render(<Probe navigate={vi.fn()} />)
    await settle()
    const onToken = h.listeners.registration as unknown as (t: { value: string }) => Promise<void>
    await act(async () => { await onToken({ value: token }) })
    expect(localStorage.getItem(PUSH_TOKEN_KEY)).toBe(token)
  }

  it('drops this server\'s registration once, when the page comes back', async () => {
    await registeredWith('tok-1')
    h.permission = 'denied'
    await comeBack()
    expect(unsubscribePush).toHaveBeenCalledTimes(1)
    expect(unsubscribePush).toHaveBeenCalledWith('tok-1')
    expect(localStorage.getItem(PUSH_TOKEN_KEY)).toBeNull()
    await comeBack('resume')
    expect(unsubscribePush).toHaveBeenCalledTimes(1)
    expect(h.requested).toBe(0)
  })

  it('registers again after a later grant, with its listeners added once', async () => {
    await registeredWith('tok-1')
    h.permission = 'denied'
    await comeBack()
    h.permission = 'granted'
    await comeBack('resume')
    expect(h.registered).toBe(2)
    expect(h.added.filter((n) => n === 'registration')).toHaveLength(1)
    expect(h.added.filter((n) => n === 'pushNotificationActionPerformed')).toHaveLength(1)
  })

  it('drops it at the next start when the app was ended in between', async () => {
    localStorage.setItem(PUSH_TOKEN_KEY, 'tok-old')
    h.permission = 'prompt'
    render(<Probe navigate={vi.fn()} />)
    await settle()
    expect(unsubscribePush).toHaveBeenCalledWith('tok-old')
    expect(localStorage.getItem(PUSH_TOKEN_KEY)).toBeNull()
    expect(h.registered).toBe(0)
  })

  it('keeps the token and asks again when the server did not answer', async () => {
    await registeredWith('tok-1')
    vi.mocked(unsubscribePush).mockResolvedValue(false)
    h.permission = 'denied'
    await comeBack()
    expect(localStorage.getItem(PUSH_TOKEN_KEY)).toBe('tok-1')
    vi.mocked(unsubscribePush).mockResolvedValue(true)
    await comeBack()
    expect(unsubscribePush).toHaveBeenCalledTimes(2)
    expect(localStorage.getItem(PUSH_TOKEN_KEY)).toBeNull()
  })

  it('a granted page that comes back registers nothing more', async () => {
    await registeredWith('tok-1')
    await comeBack()
    await comeBack('resume')
    expect(h.registered).toBe(1)
    expect(unsubscribePush).not.toHaveBeenCalled()
  })

  it('after sign-out the page drops nothing and asks nothing', async () => {
    localStorage.setItem(PUSH_TOKEN_KEY, 'tok-1')
    const view = render(<Probe navigate={vi.fn()} />)
    await settle()
    view.rerender(<Probe navigate={vi.fn()} enabled={false} />)
    h.permission = 'denied'
    await comeBack()
    await comeBack('resume')
    expect(unsubscribePush).not.toHaveBeenCalled()
    expect(localStorage.getItem(PUSH_TOKEN_KEY)).toBe('tok-1')
  })

  it('a page that is not signed in never reads the permission', async () => {
    localStorage.setItem(PUSH_TOKEN_KEY, 'tok-1')
    h.permission = 'denied'
    render(<Probe navigate={vi.fn()} enabled={false} />)
    await settle()
    await comeBack()
    expect(unsubscribePush).not.toHaveBeenCalled()
    expect(h.requested).toBe(0)
  })

  it('a build without Firebase is not asked again at every return', async () => {
    h.registerFails = true
    render(<Probe navigate={vi.fn()} />)
    await settle()
    await comeBack()
    expect(h.registered).toBe(1)
  })
})
