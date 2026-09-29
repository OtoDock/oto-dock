/**
 * Per-MCP OAuth and the account connect in the Android app: the app asks
 * before opening another site, so the deep-link waiter
 * exists before the browser is asked for (a build reload waits for it), a
 * declined confirm ends the caller's wait at once, and that decline never
 * leaks into a later flow.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { installFakeNativeChannel } from './fixtures/nativeChannel'

const h = vi.hoisted(() => ({ native: true, open: vi.fn(async () => {}) }))
vi.mock('@capacitor/core', () => ({ Capacitor: { isNativePlatform: () => h.native } }))
vi.mock('@capacitor/browser', () => ({
  Browser: { open: h.open, addListener: vi.fn(async () => ({ remove: () => {} })) },
}))

const URL_ = 'https://accounts.google.com/o/oauth2/v2/auth?state=s'
const settle = () => new Promise((r) => setTimeout(r, 0))

beforeEach(() => {
  vi.resetModules()
  h.native = true
  h.open.mockClear()
})
afterEach(() => {
  delete (window as unknown as { OtoDockNative?: unknown }).OtoDockNative
})

describe('openOAuthWindow in the app, deep-link mode', () => {
  it('waits for the link from before the browser is asked for', async () => {
    const ch = installFakeNativeChannel()
    const oauth = await import('@/lib/oauth')
    const opened = oauth.openOAuthWindow(URL_, 'google-oauth', undefined, { useDeepLink: true })
    await settle()
    expect(oauth.deepLinkPending()).toBe(true)
    expect(ch.posted.map((p) => [p.m, p.a])).toEqual([['openAuthBrowser', [URL_, 'oauth']]])
    ch.reply(ch.posted[0].id!, true)
    await expect(opened).resolves.toBe(true)
    const link = oauth.waitForDeepLink()
    ;(window as unknown as { _handleDeepLink: (u: string) => void })._handleDeepLink('otodock://oauth/google/complete')
    await expect(link).resolves.toBe('otodock://oauth/google/complete')
    expect(oauth.deepLinkPending()).toBe(false)
  })

  it('a declined confirm ends the wait at once, and only that wait', async () => {
    installFakeNativeChannel({ openAuthBrowser: false })
    const oauth = await import('@/lib/oauth')
    await expect(oauth.openOAuthWindow(URL_, 'google-oauth', undefined, { useDeepLink: true }))
      .resolves.toBe(false)
    await expect(oauth.waitForDeepLink()).rejects.toThrow(/cancelled/i)
    expect(oauth.deepLinkPending()).toBe(false)
    let settled = false
    const later = oauth.waitForDeepLink(60_000).then(() => { settled = true }, () => { settled = true })
    await settle()
    expect(settled).toBe(false)
    ;(window as unknown as { _handleDeepLink: (u: string) => void })._handleDeepLink('otodock://oauth/x')
    await later
    expect(settled).toBe(true)
  })

  it('without the channel, the in-app browser tab opens as before', async () => {
    const oauth = await import('@/lib/oauth')
    await expect(oauth.openOAuthWindow(URL_, 'google-oauth', undefined, { useDeepLink: true }))
      .resolves.toBe(true)
    expect(h.open).toHaveBeenCalledWith({ url: URL_, presentationStyle: 'popover' })
  })
})
