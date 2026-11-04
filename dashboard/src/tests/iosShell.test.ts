// The iOS app's channel shim (ios/App/App/OtoDockNative.js, a user script
// the app injects into the main frame, called with the active origin): the
// Android object's shape over WebKit's reply-capable message handler, so
// lib/nativeBridge.ts posts and listens the same way on both apps, present
// on the active origin's pages and the bundled setup page only, and the
// setup page's synchronous object with the installs list the app pushes
// after a load.
// @vitest-environment node
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import { describe, expect, it } from 'vitest'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SHIM = readFileSync(join(ROOT, 'ios', 'App', 'App', 'OtoDockNative.js'), 'utf8')

type Posted = string[]

interface FakeWindow {
  webkit?: { messageHandlers: { OtoDockNative: { postMessage(message: string): Promise<unknown> } } }
  OtoDockNative?: { postMessage(message: string): void; addEventListener(type: string, fn: (e: { data: unknown }) => void): void }
  Android?: Record<string, (...args: string[]) => unknown>
  __otodockSetInstalls?: (list: unknown) => void
}

interface FakeLocation { protocol: string; pathname: string; origin: string }

/** The active installation the app bakes into the script's call. */
const ACTIVE = 'https://agents.example.com'

/** The script as the app injects it: the file, called with the active
 *  origin (an empty string when no installation is active). */
const injected = (activeOrigin: string) => `${SHIM}(${JSON.stringify(activeOrigin)});`

/** Run the shim against a fake window and location; `answer` is what the
 *  app replies to a posted message (null for a call with no reply). */
function run(location: FakeLocation, answer: (m: string) => unknown = () => null, activeOrigin = ACTIVE) {
  const posted: Posted = []
  const window: FakeWindow = {
    webkit: { messageHandlers: { OtoDockNative: { postMessage: (m) => { posted.push(m); return Promise.resolve(answer(m)) } } } },
  }
  vm.runInNewContext(injected(activeOrigin), { window, location })
  return { window, posted }
}

const DASHBOARD: FakeLocation = { protocol: 'https:', pathname: '/chat/pa/1', origin: ACTIVE }
const SETUP: FakeLocation = { protocol: 'capacitor:', pathname: '/setup.html', origin: 'capacitor://localhost' }
const tick = () => new Promise((r) => setTimeout(r, 0))

describe('the channel object', () => {
  it('posts the string on the handler and takes nothing else', () => {
    const { window, posted } = run(DASHBOARD)
    const channel = window.OtoDockNative!
    channel.postMessage('{"m":"dashboardReady","a":[]}')
    expect(posted).toEqual(['{"m":"dashboardReady","a":[]}'])
    expect(() => channel.postMessage(42 as unknown as string)).toThrow('a message is a string')
  })

  it('delivers the app\'s reply to the listeners as a message event', async () => {
    const { window } = run(DASHBOARD, (m) => (m.includes('"id":7') ? '{"id":7,"r":true}' : null))
    const channel = window.OtoDockNative!
    const got: unknown[] = []
    channel.addEventListener('message', (e) => got.push(e.data))
    channel.addEventListener('click', () => got.push('never'))
    channel.postMessage('{"m":"switchToInstall","a":["p2","/"],"id":7}')
    channel.postMessage('{"m":"setStreaming","a":[true]}')
    await tick()
    expect(got).toEqual(['{"id":7,"r":true}'])
  })

  it('is fixed on the window and absent without the handler', () => {
    const { window } = run(DASHBOARD)
    expect(Object.getOwnPropertyDescriptor(window, 'OtoDockNative')?.writable).toBe(false)
    const bare: FakeWindow = {}
    vm.runInNewContext(injected(ACTIVE), { window: bare, location: DASHBOARD })
    expect(bare.OtoDockNative).toBeUndefined()
    expect(bare.Android).toBeUndefined()
  })

  it('exists on the active origin\'s pages only, never on another origin\'s', () => {
    // A sign-in's sibling host, another site, the same host over http: no object, so none can tell the app is there.
    for (const origin of ['https://auth.example.com', 'https://evil.example.net', 'http://agents.example.com']) {
      const { window } = run({ protocol: 'https:', pathname: '/', origin })
      expect(window.OtoDockNative, origin).toBeUndefined()
      expect(window.Android, origin).toBeUndefined()
    }
    const { window } = run({ protocol: 'https:', pathname: '/login', origin: ACTIVE })
    expect(window.OtoDockNative).toBeDefined()
  })

  it('exists nowhere but the setup page when no installation is active', () => {
    expect(run(DASHBOARD, () => null, '').window.OtoDockNative).toBeUndefined()
    const setup = run(SETUP, () => null, '').window
    expect(setup.OtoDockNative).toBeDefined()
    expect(setup.Android).toBeDefined()
  })

  it('the file itself does not run: the app appends the call', () => {
    const bare: FakeWindow = { webkit: { messageHandlers: { OtoDockNative: { postMessage: () => Promise.resolve(null) } } } }
    vm.runInNewContext(SHIM, { window: bare, location: DASHBOARD })
    expect(bare.OtoDockNative).toBeUndefined()
    expect(SHIM.trimEnd().endsWith('})')).toBe(true)
  })

  it('offers no setup object on a dashboard page', () => {
    const { window } = run(DASHBOARD)
    expect(window.Android).toBeUndefined()
    expect(window.__otodockSetInstalls).toBeUndefined()
  })
})

describe('the setup page\'s object', () => {
  const INSTALLS = [
    { id: 'a', url: 'https://a.example.com', label: 'A', favorite: true, active: true },
    { id: 'b', url: 'https://b.example.com', label: 'B', favorite: false, active: false },
  ]

  it('answers the installs the app pushed, an empty list before', () => {
    const { window } = run(SETUP)
    const android = window.Android!
    expect(android.getInstallations()).toBe('[]')
    window.__otodockSetInstalls!(INSTALLS)
    expect(JSON.parse(android.getInstallations() as string)).toEqual(INSTALLS)
    window.__otodockSetInstalls!('not a list')
    expect(android.getInstallations()).toBe('[]')
  })

  it('posts each call as a setup method and moves the star locally', () => {
    const { window, posted } = run(SETUP)
    const android = window.Android!
    window.__otodockSetInstalls!(INSTALLS.map((i) => ({ ...i })))
    android.setFavorite('b')
    const list = JSON.parse(android.getInstallations() as string) as typeof INSTALLS
    expect(list.map((i) => i.favorite)).toEqual([false, true])
    android.switchFromSetup('b')
    android.addInstallation('https://c.example.com')
    android.cancelAddInstallation()
    android.openPrivacyPolicy()
    expect(posted.map((p) => JSON.parse(p))).toEqual([
      { m: 'setup.setFavorite', a: ['b'] },
      { m: 'setup.switchFromSetup', a: ['b'] },
      { m: 'setup.addInstallation', a: ['https://c.example.com'] },
      { m: 'setup.cancelAddInstallation', a: [] },
      { m: 'setup.openPrivacyPolicy', a: [] },
    ])
  })

  it('exists only on the bundled page, never on a server\'s own setup.html', () => {
    const served = run({ protocol: 'https:', pathname: '/setup.html', origin: ACTIVE })
    expect(served.window.Android).toBeUndefined()
    expect(served.window.OtoDockNative).toBeDefined()
    const other = run({ protocol: 'capacitor:', pathname: '/index.html', origin: 'capacitor://localhost' })
    expect(other.window.Android).toBeUndefined()
    expect(other.window.OtoDockNative).toBeUndefined()
  })
})
