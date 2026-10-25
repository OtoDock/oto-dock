// The boot screen: the shell loads it with no inline script, its colours are
// the tokens', its mark and wordmark are the master's, the names the app
// calls are the app's, and the hand-over runs in the right order on the web
// and in the app.
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createElement } from 'react'
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { cssColor, model } from '../../design/build.mjs'
import { installFakeNativeChannel } from './fixtures/nativeChannel'

vi.mock('@/hooks/useFcmPush', () => ({ useFcmPush: () => {} }))
vi.mock('@/hooks/useWakeWord', () => ({ useWakeWord: () => {} }))
vi.mock('@/hooks/useBuildWatch', () => ({ useBuildWatch: () => {} }))
vi.mock('@/hooks/useOpenAppToast', () => ({ useOpenAppToast: () => null }))
vi.mock('@/components/WakeDiagBadge', () => ({ default: () => null }))
const auth = vi.hoisted(() => ({
  user: null as unknown, loading: true, authConfig: null as unknown, loginError: null as string | null,
  login() {}, logout() {}, setUser() {}, refreshUser: async () => {},
}))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => auth }))

const ROOT = join(__dirname, '..', '..')
const html = readFileSync(join(ROOT, 'index.html'), 'utf8')
const bootJs = readFileSync(join(ROOT, 'public', 'boot.js'), 'utf8')
const bridgeTs = readFileSync(join(ROOT, 'src', 'lib', 'nativeBridge.ts'), 'utf8')
const head = html.slice(html.indexOf('<head>'), html.indexOf('</head>'))
const style = head.slice(head.indexOf('<style>'), head.indexOf('</style>'))
const BOOT_DONE = 'otodock:bootdone'

describe('the shell', () => {
  it('carries no inline script or handler and loads boot.js synchronously in the head', () => {
    expect(head).toMatch(/<script src="\/boot\.js"><\/script>/)
    expect(head).not.toMatch(/<script(?![^>]*\bsrc=)[^>]*>/)
    expect(html.slice(html.indexOf('<body>'))).not.toMatch(/<script(?![^>]*type="module")[^>]*>/)
    expect(html).not.toMatch(/\son[a-z]+\s*=/i)
    expect(head.indexOf('boot.js')).toBeLessThan(html.indexOf('src="/src/main.tsx"'))
  })

  it('shows the overlay only once boot.js has marked the page', () => {
    expect(style).toMatch(/#boot \{ display: none; \}/)
    expect(style).toMatch(/html\[data-boot\] #boot \{/)
    expect(style).toContain('#boot.is-done { opacity: 0; pointer-events: none; transition: opacity 200ms cubic-bezier(0.3, 0, 1, 1); }')
    expect(style).toContain('animation: boot-orbit 1100ms cubic-bezier(0.2, 0, 0, 1) both paused;')
  })

  it('draws the master mark and the lockup wordmark', () => {
    const d = html.match(/<path class="boot-ink" fill-rule="evenodd" d="([^"]+)"/)![1]
    // The rim the light runs along is the outline alone: the silhouette's first subpath.
    expect(html.match(/<mask id="boot-rim"[^>]*><path d="([^"]+)"/)![1]).toBe(d.slice(0, d.indexOf('Z') + 1))
    const master = join(ROOT, 'design', 'brand', 'otodock-mark.svg')
    if (!existsSync(master)) return
    const silhouette = readFileSync(master, 'utf8').match(/id="silhouette"[^>]*\sd="([^"]+)"/)![1]
    expect(d).toBe(silhouette)
    const lockup = readFileSync(join(ROOT, 'design', 'brand', 'otodock-lockup.svg'), 'utf8')
    const word = lockup.match(/id="wordmark"[^>]*\sd="([^"]+)"/)![1]
      .replace(/-?\d+(\.\d+)?/g, (n) => String(Math.round(Number(n) * 100) / 100))
    expect(html.match(/class="boot-word"[^>]*><path class="boot-ink" d="([^"]+)"/)![1]).toBe(word)
  })
})

describe('the colours', () => {
  const m = model()
  const v = (name: string, mode: 'light' | 'dark') => cssColor(m.resolve(name, mode) as string)

  it('in boot.js are the tokens', () => {
    expect(bootJs).toContain(`light: '${v('surface.page', 'light')}'`)
    expect(bootJs).toContain(`dark: '${v('surface.page', 'dark')}'`)
    expect(bootJs).toContain(`FIELD = '${v('color.blue.800', 'light')}'`)
  })

  it('in the style block are the tokens', () => {
    expect(style).toContain(`linear-gradient(${v('color.blue.800', 'light')}, #174067)`)
    expect(style).toContain(`html[data-boot="web"] { --boot-bg: ${v('surface.page', 'light')}; --boot-ink: ${v('brand.mark', 'light')};`)
    expect(style).toContain(`html[data-boot="web"].dark { --boot-bg: ${v('surface.page', 'dark')}; --boot-ink: ${v('brand.mark', 'dark')};`)
    expect(style).toContain(`html[data-boot="app"] { --boot-bg: linear-gradient(${v('color.blue.800', 'light')}, #174067); --boot-ink: ${v('color.white', 'light')}; --boot-light: ${v('color.amber.100', 'light')}; }`)
  })

  it('names no colour outside the tokens', () => {
    // #174067 is the field's deep end (blue.800 with the HSL lightness six
    // points lower), derived by the private generator, not a token.
    const values = new Set(['#174067'])
    for (const name of ['surface.page', 'brand.mark', 'color.blue.800', 'color.amber.100', 'color.white']) {
      for (const mode of ['light', 'dark'] as const) values.add(v(name, mode))
    }
    const hexes = [...(style + bootJs).matchAll(/#[0-9a-f]{3,8}\b/gi)].map((h) => h[0].toLowerCase())
    expect(hexes.length).toBeGreaterThan(5)
    for (const hex of hexes) expect(values, hex).toContain(hex)
  })
})

describe('the contract with the app', () => {
  // The app's sources are read only where the Android tree is present: the
  // public cut stages it out with the mobile apps.
  const mainPath = join(ROOT, 'android', 'app', 'src', 'main', 'java', 'com', 'otodock', 'MainActivity.java')

  it('names the method the page posts', () => {
    expect(bridgeTs).toContain("| 'firstScreen'")
  })

  it.skipIf(!existsSync(mainPath))('names what the app calls and the page answers', () => {
    const main = readFileSync(mainPath, 'utf8')
    expect(main).toContain('case "firstScreen":')
    expect(main).toContain('case "setContainerColor":')
    expect(bootJs).toContain("m: 'setContainerColor', a: [FIELD]")
    expect(main).toContain("window.__otodockSplash && window.__otodockSplash('")
    expect(bootJs).toContain('window.__otodockSplash = splash')
    for (const cue of ['leaving', 'gone']) {
      expect(main).toContain(`tellSplash(wv, "${cue}")`)
      expect(bootJs).toContain(`'${cue}'`)
    }
    for (const name of ['--oto-bar-top', '--oto-bar-bottom']) {
      expect(main).toContain(name)
      expect(style).toContain(name)
    }
  })
})

type Boot = { done: boolean; finish(): void }
type Shell = Window & { __otodockBoot?: Boot; __otodockSplash?: (s: string) => void }
const shell = window as Shell

function loadBoot(opts: { app?: boolean; theme?: string | null; dark?: boolean; channel?: Window['OtoDockNative'] } = {}) {
  document.documentElement.removeAttribute('data-boot')
  document.documentElement.className = ''
  document.head.innerHTML = '<meta name="theme-color" content="#faf9f9">'
  document.body.innerHTML = '<div id="boot"></div><div id="root"></div>'
  if (opts.theme) localStorage.setItem('theme', opts.theme)
  else localStorage.removeItem('theme')
  window.matchMedia = vi.fn(() => ({ matches: !!opts.dark, addEventListener() {}, removeEventListener() {} })) as never
  if (opts.channel) shell.OtoDockNative = opts.channel
  else if (opts.app) shell.OtoDockNative = { postMessage() {}, addEventListener() {} }
  else delete shell.OtoDockNative
  delete shell.__otodockBoot
  delete shell.__otodockSplash
  new Function(bootJs)()
}

const overlay = () => document.getElementById('boot')
const shown = () => document.documentElement.classList.contains('boot-shown')

describe('boot.js', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); delete shell.OtoDockNative })

  it('applies the stored theme before paint and names the page colour', () => {
    loadBoot({ theme: 'dark' })
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(document.documentElement.getAttribute('data-boot')).toBe('web')
    expect(document.querySelector('meta[name="theme-color"]')!.getAttribute('content')).toBe('#0d1117')
  })

  it('follows the system while the theme is system or unset', () => {
    loadBoot({ theme: 'system', dark: true })
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    loadBoot({ theme: null, dark: false })
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    expect(document.querySelector('meta[name="theme-color"]')!.getAttribute('content')).toBe('#faf9f9')
  })

  it('on the web runs the orbit from paint and leaves at the finish', () => {
    loadBoot({})
    expect(shown()).toBe(true)
    const done = vi.fn()
    document.addEventListener(BOOT_DONE, done)
    shell.__otodockBoot!.finish()
    expect(overlay()!.classList.contains('is-done')).toBe(true)
    expect(shell.__otodockBoot!.done).toBe(true)
    expect(done).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(220)
    expect(overlay()).toBeNull()
    expect(document.documentElement.hasAttribute('data-boot')).toBe(false)
  })

  it('in the app paints the field and waits for the splash to leave', () => {
    loadBoot({ app: true, theme: 'light' })
    expect(document.documentElement.getAttribute('data-boot')).toBe('app')
    expect(document.querySelector('meta[name="theme-color"]')!.getAttribute('content')).toBe('#1d4f80')
    expect(shown()).toBe(false)
    shell.__otodockSplash!('leaving')
    expect(shown()).toBe(true)
    shell.__otodockBoot!.finish()
    expect(overlay()!.classList.contains('is-done')).toBe(false)
    shell.__otodockSplash!('gone')
    expect(overlay()!.classList.contains('is-done')).toBe(true)
  })

  it('in the app posts the field for an older app before anything else', () => {
    const fake = installFakeNativeChannel()
    loadBoot({ app: true, channel: shell.OtoDockNative })
    expect(fake.posted).toEqual([{ m: 'setContainerColor', a: ['#1d4f80'] }])
  })

  it('in the app leaves at once when the splash was already gone', () => {
    loadBoot({ app: true })
    shell.__otodockSplash!('gone')
    expect(shown()).toBe(true)
    shell.__otodockBoot!.finish()
    expect(overlay()!.classList.contains('is-done')).toBe(true)
  })

  it('in an app without the cue runs the orbit and leaves on its own', () => {
    loadBoot({ app: true })
    vi.advanceTimersByTime(3000)
    expect(shown()).toBe(true)
    shell.__otodockBoot!.finish()
    vi.advanceTimersByTime(1499)
    expect(overlay()!.classList.contains('is-done')).toBe(false)
    vi.advanceTimersByTime(1)
    expect(overlay()!.classList.contains('is-done')).toBe(true)
  })

  it('releases a page whose script never runs', () => {
    loadBoot({})
    vi.advanceTimersByTime(29999)
    expect(overlay()).not.toBeNull()
    vi.advanceTimersByTime(1 + 220)
    expect(overlay()).toBeNull()
  })
})

describe('the signal', () => {
  let boot: typeof import('@/lib/device/boot')

  beforeEach(async () => {
    vi.resetModules()
    document.body.innerHTML = '<div id="root"></div>'
    delete shell.__otodockBoot
    delete shell.OtoDockNative
    boot = await import('@/lib/device/boot')
  })
  afterEach(() => { vi.useRealTimers(); delete shell.OtoDockNative })

  it('a tree is a screen only with no placeholder and something to see', () => {
    const root = document.getElementById('root')!
    expect(boot.isScreen(root)).toBe(false)
    root.innerHTML = '<div></div>'
    expect(boot.isScreen(root)).toBe(false)
    root.innerHTML = `<div ${boot.BOOT_WAIT_ATTR}=""><p>Loading...</p></div>`
    expect(boot.isScreen(root)).toBe(false)
    root.innerHTML = '<p>Sign in</p>'
    expect(boot.isScreen(root)).toBe(true)
    root.innerHTML = '<div><svg></svg></div>'
    expect(boot.isScreen(root)).toBe(true)
  })

  it('finishBoot posts firstScreen on the channel once', () => {
    const ch = installFakeNativeChannel()
    boot.finishBoot()
    boot.finishBoot()
    expect(ch.methods()).toEqual(['firstScreen'])
  })

  it('onBootDone runs at once with no boot up and after the event with one', () => {
    const now = vi.fn()
    boot.onBootDone(now)
    expect(now).toHaveBeenCalledTimes(1)
    const up: Boot = { done: false, finish() { up.done = true; document.dispatchEvent(new Event(BOOT_DONE)) } }
    shell.__otodockBoot = up
    const later = vi.fn()
    boot.onBootDone(later)
    expect(later).not.toHaveBeenCalled()
    up.finish()
    expect(later).toHaveBeenCalledTimes(1)
    const after = vi.fn()
    boot.onBootDone(after)
    expect(after).toHaveBeenCalledTimes(1)
  })

  it('the watcher ends the boot at the first commit that is a screen', async () => {
    const up: Boot = { done: false, finish: vi.fn() }
    shell.__otodockBoot = up
    const root = document.getElementById('root')!
    boot.watchFirstScreen(root)
    root.innerHTML = `<div ${boot.BOOT_WAIT_ATTR}=""><p>Loading...</p></div>`
    await Promise.resolve()
    expect(up.finish).not.toHaveBeenCalled()
    root.innerHTML = '<p>Sign in</p>'
    await Promise.resolve()
    expect(up.finish).toHaveBeenCalledTimes(1)
    root.innerHTML = '<p>Another</p>'
    await Promise.resolve()
    boot.finishBoot()
    expect(up.finish).toHaveBeenCalledTimes(1)
  })

  it('the watcher lets a page with no screen go after its wait', () => {
    vi.useFakeTimers()
    const up: Boot = { done: false, finish: vi.fn() }
    shell.__otodockBoot = up
    boot.watchFirstScreen(document.getElementById('root')!)
    vi.advanceTimersByTime(9999)
    expect(up.finish).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(up.finish).toHaveBeenCalledTimes(1)
  })

  it("the gate's placeholders are not screens and its redirect notice is", async () => {
    const { default: RequireAuth } = await import('@/components/RequireAuth')
    const { MemoryRouter, Route, Routes } = await import('react-router-dom')
    const ui = () => createElement(MemoryRouter, null,
      createElement(Routes, null,
        createElement(Route, { element: createElement(RequireAuth) },
          createElement(Route, { path: '/', element: createElement('div', null, 'home') }))))
    auth.loading = true
    auth.authConfig = null
    const { container, rerender } = render(ui())
    expect(boot.isScreen(container)).toBe(false)
    auth.loading = false
    rerender(ui())
    expect(boot.isScreen(container)).toBe(false)
    auth.authConfig = { auth_provider_bypass: true, oidc_enabled: true, setup_required: false }
    rerender(ui())
    expect(boot.isScreen(container)).toBe(true)
  })

  it('the theme reaches the app only once the boot screen leaves', async () => {
    const ch = installFakeNativeChannel()
    window.matchMedia = vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never
    localStorage.removeItem('theme')
    document.documentElement.style.setProperty('--ot-surface-page', '#faf9f9')
    const up: Boot = { done: false, finish() { up.done = true; document.dispatchEvent(new Event(BOOT_DONE)) } }
    shell.__otodockBoot = up
    const { ThemeProvider } = await import('@/contexts/ThemeContext')
    await act(async () => { render(createElement(ThemeProvider, null, createElement('div'))) })
    expect(ch.methods()).not.toContain('setContainerColor')
    await act(async () => { up.finish() })
    const posts = ch.posted.filter((p) => p.m === 'setContainerColor')
    expect(posts).toHaveLength(1)
    expect(posts[0].a).toEqual(['#faf9f9'])
  })
})
