/**
 * The Android app's bridge: the dashboard reaches native
 * code only through the origin-scoped `OtoDockNative` channel in
 * `lib/nativeBridge.ts`. Off the app every wrapper is a no-op; in the app every
 * call posts on the channel, and nothing reads the retired `window.Android`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'

import { cssColor, model } from '../../design/build.mjs'
import { act, render } from '@testing-library/react'
import {
  APP_PRIVACY_POLICY_URL, askNative, callNative, hasNativeBridge, saveNativeFile,
  setNativeAuthInProgress, setNativeSwitchBusy,
} from '@/lib/nativeBridge'
import { installFakeNativeChannel, watchRetiredAndroidName } from './fixtures/nativeChannel'

afterEach(() => {
  delete (window as unknown as { OtoDockNative?: unknown }).OtoDockNative
  vi.useRealTimers()
})

describe('off the app', () => {
  it('every wrapper is a no-op and a question resolves at once with no answer', async () => {
    expect(hasNativeBridge()).toBe(false)
    expect(callNative('dashboardReady')).toBe(false)
    expect(saveNativeFile('QUJD', 'a.png', 'image/png')).toBe(false)
    setNativeAuthInProgress(true)
    setNativeSwitchBusy(true)
    setNativeSwitchBusy(false)
    vi.useFakeTimers()
    const answer = askNative('getInstallations')
    expect(vi.getTimerCount()).toBe(0)
    await expect(answer).resolves.toBeUndefined()
  })
})

describe('in the app', () => {
  it('posts the method and its arguments on the channel object', () => {
    const ch = installFakeNativeChannel()
    expect(hasNativeBridge()).toBe(true)
    expect(callNative('setStreaming', true)).toBe(true)
    setNativeAuthInProgress(false)
    setNativeSwitchBusy(true)
    expect(ch.posted).toEqual([
      { m: 'setStreaming', a: [true] },
      { m: 'setAuthInProgress', a: [false] },
      { m: 'setSwitchBusy', a: [true] },
    ])
  })

  it('a saved file travels as a header and a raw body', () => {
    const ch = installFakeNativeChannel()
    expect(saveNativeFile('QUJD', 'photo.png', 'image/png')).toBe(true)
    expect(ch.posted).toEqual([{ m: 'saveImageFromBase64', a: ['photo.png', 'image/png'], body: 'QUJD' }])
  })

  it('a question is answered by its own id', async () => {
    const ch = installFakeNativeChannel()
    const first = askNative<boolean>('switchToInstall', ['p1', '/x'])
    const second = askNative<unknown[]>('getInstallations')
    const [a, b] = ch.posted
    ch.reply(b.id!, [{ id: 'i1' }])
    ch.reply(a.id!, true)
    await expect(first).resolves.toBe(true)
    await expect(second).resolves.toEqual([{ id: 'i1' }])
  })

  it('a question nobody answers resolves empty after its timeout', async () => {
    vi.useFakeTimers()
    installFakeNativeChannel()
    const answer = askNative('getInstallations', [], 3000)
    vi.advanceTimersByTime(3000)
    await expect(answer).resolves.toBeUndefined()
  })
})

describe('the call sites', () => {
  let retired: ReturnType<typeof watchRetiredAndroidName>
  beforeEach(() => { retired = watchRetiredAndroidName() })
  afterEach(() => retired.restore())

  it('post on the channel and never read window.Android', async () => {
    const ch = installFakeNativeChannel({ getInstallations: [] })
    const { triggerDownload } = await import('@/components/chat/media/imageDownload')
    await triggerDownload({ imageData: 'QUJD', mimeType: 'image/png', caption: 'plan' } as never, 0)
    const { saveWakeDiag, setWakeDiag, wakeDiagFrames } = await import('@/audio/wakeDiag')
    setWakeDiag(true)
    wakeDiagFrames(new Float32Array(1600))
    saveWakeDiag()
    setWakeDiag(false)
    window.matchMedia = vi.fn(() => ({
      matches: false, addEventListener: () => {}, removeEventListener: () => {},
    })) as unknown as typeof window.matchMedia
    // The theme reads the page colour from the stylesheet, absent here.
    document.documentElement.style.setProperty('--ot-surface-page', '#faf9f9')
    const { ThemeProvider } = await import('@/contexts/ThemeContext')
    const { default: AppSettingsModal } = await import('@/components/chat/AppSettingsModal')
    await act(async () => {
      render(<ThemeProvider><AppSettingsModal open onClose={() => {}} /></ThemeProvider>)
    })
    const methods = ch.methods()
    expect(methods).toContain('saveImageFromBase64')
    expect(methods).toContain('setContainerColor')
    expect(methods).toContain('getInstallations')
    expect(retired.reads()).toBe(0)
  })
})

describe('the source', () => {
  // Every native call goes through lib/nativeBridge.ts: no other file names the
  // retired object or the channel (comments removed, tests excluded).
  const SRC = join(__dirname, '..')
  const RETIRED = /\.Android\b|\[\s*['"`]Android['"`]\s*\]|\bAndroid\?\.|\bAndroid\.[a-z]|\bOtoDockNative\b/

  function files(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const path = join(dir, name)
      if (statSync(path).isDirectory()) return name === 'tests' ? [] : files(path)
      return /\.(ts|tsx)$/.test(name) ? [path] : []
    })
  }

  it('names the bridge only in lib/nativeBridge.ts', () => {
    const printer = ts.createPrinter({ removeComments: true })
    const offenders = files(SRC)
      .filter((path) => relative(SRC, path) !== join('lib', 'nativeBridge.ts'))
      .filter((path) => {
        const sf = ts.createSourceFile(path, readFileSync(path, 'utf8'), ts.ScriptTarget.Latest, false,
          path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
        return RETIRED.test(printer.printFile(sf))
      })
      .map((path) => relative(SRC, path))
    expect(offenders).toEqual([])
  })
})

describe('the setup page', () => {
  // The app's bundled first-run page (public/setup.html) loads nothing from
  // another host: its fonts are the build's own copies under setup-fonts/.
  const ROOT = join(__dirname, '..', '..')
  const page = readFileSync(join(ROOT, 'public', 'setup.html'), 'utf8')

  it('loads no resource from another host', () => {
    expect(page).not.toMatch(/(?:src|href)\s*=\s*["']?\s*(?:https?:)?\/\//i)
    expect(page).not.toMatch(/url\(\s*["']?\s*(?:https?:)?\/\//i)
    expect(page).not.toMatch(/@import/i)
  })

  it('names only fonts the build copies beside it', () => {
    const fonts = [...page.matchAll(/url\(setup-fonts\/([\w-]+\.woff2)\)/g)].map((m) => m[1])
    expect(fonts.length).toBeGreaterThan(0)
    expect(page).not.toMatch(/url\((?!setup-fonts\/)/)
    const config = readFileSync(join(ROOT, 'vite.config.ts'), 'utf8')
    // One copy entry per family; a one-member brace is literal in the glob,
    // so a weight may be written plain.
    const copied = new Set<string>()
    const entries = config.matchAll(/src: 'node_modules\/@fontsource\/(\w+)\/files\/\1-\{([\w,]+)\}-(\{[\d,]+\}|\d+)-normal\.woff2',\s*dest: 'setup-fonts'/g)
    for (const [, family, subsets, weights] of entries) {
      for (const subset of subsets.split(',')) {
        for (const weight of weights.replace(/[{}]/g, '').split(',')) copied.add(`${family}-${subset}-${weight}-normal.woff2`)
      }
    }
    expect(copied.size).toBeGreaterThan(0)
    for (const font of fonts) expect(copied, font).toContain(font)
  })

  it('paints the tokens in both themes', () => {
    const m = model()
    const values = new Set<string>()
    for (const name of ['surface.page', 'surface.card', 'text.primary', 'text.secondary', 'text.tertiary',
      'border.default', 'border.strong', 'action.default', 'action.hover', 'action.fg', 'focus.ring', 'brand.mark', 'status.danger.bg',
      'status.danger.fg', 'status.danger.border', 'status.success.fg', 'status.warning.solid']) {
      for (const mode of ['light', 'dark'] as const) values.add(cssColor(m.resolve(name, mode) as string))
    }
    const hexes = [...page.matchAll(/#[0-9a-f]{6}\b/gi)].map((h) => h[0].toLowerCase())
    expect(hexes.length).toBeGreaterThan(10)
    for (const hex of hexes) expect(values, hex).toContain(hex)
    expect(page).toContain('@media (prefers-color-scheme: dark)')
    expect(page).toMatch(/<meta name="color-scheme" content="light dark">/)
    expect(page).not.toContain('logo.png')
  })

  it('shows the privacy link only when the app offers to open it', () => {
    expect(page).toMatch(/typeof Android\.openPrivacyPolicy === 'function'/)
    expect(page).toMatch(/id="privacy-link"[^>]*class="privacy-link"/)
    expect(page).toMatch(/\.privacy-link \{\s*display: none;/)
  })

  // The native constant is checked only where the Android tree is present: the
  // public cut stages it out with the mobile apps.
  const policyPath = join(ROOT, 'android', 'app', 'src', 'main', 'java', 'com', 'otodock', 'NativeBridgePolicy.java')
  it.skipIf(!existsSync(policyPath))('names the same privacy policy as the app', () => {
    const policy = readFileSync(policyPath, 'utf8')
    expect(policy).toContain(`PRIVACY_POLICY_URL = "${APP_PRIVACY_POLICY_URL}";`)
  })
})
