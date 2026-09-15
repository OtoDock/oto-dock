import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { BUILD_META_NAME, _setReloadForTests, canReloadNow, noteServerBuild, pageBuildId } from '@/lib/buildId'
import { waitForDeepLink } from '@/lib/oauth'
import { setNativeSwitchBusy } from '@/lib/nativeBridge'
import { buildIdTag, computeBuildId, stampBuildId } from '../../build/buildId'

vi.mock('@/lib/chatUploadQueue', () => ({ chatUploadActive: vi.fn(() => false) }))
import { chatUploadActive } from '@/lib/chatUploadQueue'

function setPageBuild(id: string | null) {
  document.querySelector(`meta[name="${BUILD_META_NAME}"]`)?.remove()
  if (id !== null) {
    const m = document.createElement('meta')
    m.setAttribute('name', BUILD_META_NAME)
    m.setAttribute('content', id)
    document.head.appendChild(m)
  }
}

describe('lib/buildId', () => {
  let reload: ReturnType<typeof vi.fn<() => void>>
  beforeEach(() => {
    reload = vi.fn<() => void>()
    _setReloadForTests(reload)
    sessionStorage.clear()
    setPageBuild('b1')
    vi.spyOn(console, 'log').mockImplementation(() => {})
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })
  afterEach(() => {
    _setReloadForTests(null)
    setPageBuild(null)
    setNativeSwitchBusy(false)
    vi.restoreAllMocks()
  })

  it('reads its own stamp from the page', () => {
    expect(pageBuildId()).toBe('b1')
    setPageBuild(null)
    expect(pageBuildId()).toBe('')
  })

  it('does nothing without a stamp on either side, or when they match', () => {
    expect(noteServerBuild('')).toBe('unknown')
    expect(noteServerBuild(undefined)).toBe('unknown')
    expect(noteServerBuild('b1')).toBe('same')
    setPageBuild(null)
    expect(noteServerBuild('b2')).toBe('unknown')
    expect(reload).not.toHaveBeenCalled()
  })

  it('reloads once per server build id, and warns once instead of looping', () => {
    expect(noteServerBuild('b2')).toBe('reloading')
    expect(reload).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem('oto-build-reloaded-for')).toBe('b2')
    // The page came back still on b1 (a stale intermediary): no second reload.
    expect(noteServerBuild('b2')).toBe('stale')
    expect(noteServerBuild('b2')).toBe('stale')
    expect(reload).toHaveBeenCalledTimes(1)
    expect(console.warn).toHaveBeenCalledTimes(1)
    // A THIRD build is new again.
    expect(noteServerBuild('b3')).toBe('reloading')
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('defers while work would be lost, then reloads on the next signal', () => {
    let ready = false
    expect(noteServerBuild('b2', () => ready)).toBe('deferred')
    expect(noteServerBuild('b2', () => ready)).toBe('deferred')
    expect(reload).not.toHaveBeenCalled()
    expect(console.log).toHaveBeenCalledTimes(1) // the deferral is logged once
    ready = true
    expect(noteServerBuild('b2', () => ready)).toBe('reloading')
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('canReloadNow: an awaited deep link, a live meeting or an upload in flight defer', async () => {
    expect(canReloadNow()).toBe(true)
    const link = waitForDeepLink(10_000)
    expect(canReloadNow()).toBe(false)
    ;(window as unknown as { _handleDeepLink: (u: string) => void })._handleDeepLink('otodock://oauth/x?code=1')
    await link
    expect(canReloadNow()).toBe(true)
    setNativeSwitchBusy(true)
    expect(canReloadNow()).toBe(false)
    setNativeSwitchBusy(false)
    // The running upload counts, not only the queued ones (the queue is
    // empty while its single file is on the wire).
    vi.mocked(chatUploadActive).mockReturnValueOnce(true)
    expect(canReloadNow()).toBe(false)
    expect(canReloadNow()).toBe(true)
  })
})

describe('build/buildId (the stamp)', () => {
  let dir: string
  beforeEach(() => {
    dir = mkdtempSync(path.join(tmpdir(), 'oto-build-'))
    mkdirSync(path.join(dir, 'public', 'sub'), { recursive: true })
    writeFileSync(path.join(dir, 'public', 'wake-word-worker.js'), 'worker v1')
    writeFileSync(path.join(dir, 'public', 'sub', 'a.txt'), 'a')
  })
  afterEach(() => rmSync(dir, { recursive: true, force: true }))

  it('is deterministic and changes with the page or any public file', () => {
    const pub = path.join(dir, 'public')
    const html = '<html><head><script type="module" src="/assets/index-AAAA1111.js"></script></head></html>'
    const a = computeBuildId(html, pub)
    expect(a).toMatch(/^[0-9a-f]{16}$/)
    expect(computeBuildId(html, pub)).toBe(a)
    expect(computeBuildId(html.replace('AAAA1111', 'BBBB2222'), pub)).not.toBe(a)
    writeFileSync(path.join(pub, 'wake-word-worker.js'), 'worker v2')
    expect(computeBuildId(html, pub)).not.toBe(a)
    expect(computeBuildId(html, path.join(dir, 'missing'))).toMatch(/^[0-9a-f]{16}$/)
  })

  it('injects the meta tag into the built page only', () => {
    expect(buildIdTag('abc')).toEqual({ tag: 'meta', attrs: { name: BUILD_META_NAME, content: 'abc' }, injectTo: 'head' })
    const plugin = stampBuildId()
    expect(plugin.apply).toBe('build')
    ;(plugin.configResolved as (c: { publicDir: string }) => void)({ publicDir: path.join(dir, 'public') })
    const hook = plugin.transformIndexHtml as { order: string; handler: (html: string) => unknown }
    expect(hook.order).toBe('post')
    const html = '<html><head></head></html>'
    expect(hook.handler(html)).toEqual([buildIdTag(computeBuildId(html, path.join(dir, 'public')))])
  })
})
