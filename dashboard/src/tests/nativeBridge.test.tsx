/**
 * The Android app's bridge: the dashboard reaches native
 * code only through the origin-scoped `OtoDockNative` channel in
 * `lib/nativeBridge.ts`. Off the app every wrapper is a no-op; in the app every
 * call posts on the channel, and nothing reads the retired `window.Android`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'
import { act, render } from '@testing-library/react'
import {
  askNative, callNative, hasNativeBridge, saveNativeFile, setNativeAuthInProgress,
  setNativeSwitchBusy,
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
    const { triggerDownload } = await import('@/components/chat/media/ImageLightbox')
    await triggerDownload({ imageData: 'QUJD', mimeType: 'image/png', caption: 'plan' } as never, 0)
    const { saveWakeDiag, setWakeDiag, wakeDiagFrames } = await import('@/audio/wakeDiag')
    setWakeDiag(true)
    wakeDiagFrames(new Float32Array(1600))
    saveWakeDiag()
    setWakeDiag(false)
    window.matchMedia = vi.fn(() => ({
      matches: false, addEventListener: () => {}, removeEventListener: () => {},
    })) as unknown as typeof window.matchMedia
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
