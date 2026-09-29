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
const h = vi.hoisted(() => ({ listeners: {} as Record<string, (arg: never) => unknown> }))
vi.mock('@capacitor/core', () => ({ Capacitor: { isNativePlatform: () => true } }))
vi.mock('@capacitor/push-notifications', () => ({
  PushNotifications: {
    requestPermissions: async () => ({ receive: 'granted' }),
    register: async () => {},
    addListener: async (name: string, fn: (arg: never) => unknown) => {
      h.listeners[name] = fn
      return { remove: () => {} }
    },
  },
}))
vi.mock('@/api/notifications', () => ({ subscribePush: vi.fn(async () => {}) }))

import { useFcmPush } from '@/hooks/useFcmPush'

function Probe({ navigate }: { navigate: (p: string) => void }) {
  useFcmPush(navigate, true)
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

beforeEach(() => { h.listeners = {} })
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
