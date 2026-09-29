/**
 * The app's sign-in starts (the client half of the login state's browser
 * binding): in the Android app, each start persists the WebView's
 * cookies after the fetch that set the binding cookie and only then asks the
 * app to open the system browser; a declined confirm opens nothing and never
 * falls back to a WebView navigation. Off the app they navigate as before.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { startLogin, startOidcConfirm, startOidcLogin } from '@/api/auth'
import { installFakeNativeChannel } from './fixtures/nativeChannel'

const IDP = 'https://auth.example.com/application/o/authorize/?state=s1'
const originalLocation = window.location
let location: { href: string; assign: ReturnType<typeof vi.fn> }

function deferredFetch(body: unknown, ok = true, status = 200) {
  let release!: () => void
  const gate = new Promise<void>((r) => { release = r })
  const fetchMock = vi.fn(async (_url: string) => {
    await gate
    return { ok, status, json: async () => body } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, release }
}

const settle = () => new Promise((r) => setTimeout(r, 0))

beforeEach(() => {
  location = { href: '/login', assign: vi.fn() }
  Object.defineProperty(window, 'location', { configurable: true, value: location })
})

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
  delete (window as unknown as { Capacitor?: unknown }).Capacitor
  delete (window as unknown as { OtoDockNative?: unknown }).OtoDockNative
  vi.unstubAllGlobals()
})

function native() {
  ;(window as unknown as { Capacitor?: unknown }).Capacitor = { isNativePlatform: () => true }
}

const starts: Array<[string, () => Promise<void>, string]> = [
  ['startOidcLogin', () => startOidcLogin(), '/auth/oidc-url?mobile=true'],
  ['startLogin (bypass)', () => startLogin(), '/auth/login?mobile=true'],
  ['startOidcConfirm', () => startOidcConfirm('/apps/a1'), '/auth/confirm/oidc-url?return_to=%2Fapps%2Fa1&mobile=true'],
]

describe.each(starts)('%s in the app', (_name, start, startUrl) => {
  it('flushes the cookies after the start fetch resolved, then opens the browser', async () => {
    native()
    const ch = installFakeNativeChannel({ openAuthBrowser: true })
    const { fetchMock, release } = deferredFetch({ url: IDP })
    const done = start()
    await settle()
    expect(fetchMock.mock.calls[0][0]).toBe(startUrl)
    expect(ch.posted).toEqual([])
    release()
    await done
    expect(ch.posted.map((p) => [p.m, p.a])).toEqual([
      ['flushCookies', []],
      ['openAuthBrowser', [IDP, 'auth']],
    ])
    expect(location.href).toBe('/login')
    expect(location.assign).not.toHaveBeenCalled()
  })

  it('a declined confirm opens nothing and does not navigate the WebView', async () => {
    native()
    installFakeNativeChannel({ openAuthBrowser: false })
    const { release } = deferredFetch({ url: IDP })
    release()
    await start()
    expect(location.href).toBe('/login')
    expect(location.assign).not.toHaveBeenCalled()
  })

  it('a failed start posts nothing', async () => {
    native()
    const ch = installFakeNativeChannel({ openAuthBrowser: true })
    const { release } = deferredFetch({ detail: 'no' }, false, 503)
    release()
    await expect(start()).rejects.toThrow()
    expect(ch.posted).toEqual([])
  })

  it('an app without the channel keeps the WebView navigation', async () => {
    native()
    const { release } = deferredFetch({ url: IDP })
    release()
    await start()
    expect(location.href === IDP || location.assign.mock.calls[0]?.[0] === IDP).toBe(true)
  })
})

describe.each(starts)('%s on the web', (_name, start) => {
  it('navigates to the provider and posts nothing', async () => {
    const { release } = deferredFetch({ url: IDP })
    release()
    await start()
    expect(location.href === IDP || location.assign.mock.calls[0]?.[0] === IDP).toBe(true)
  })
})
