import { describe, it, expect, vi, beforeEach } from 'vitest'

// Platform + Custom Tab are mocked so both branches run in jsdom.
const isNative = vi.fn(() => false)
vi.mock('@capacitor/core', () => ({ Capacitor: { isNativePlatform: () => isNative() } }))
const browserOpen = vi.fn(async () => {})
vi.mock('@capacitor/browser', () => ({ Browser: { open: browserOpen } }))

import { openExternalUrl, openTerminalLink, validateBridgedUrl } from '@/lib/openExternal'

beforeEach(() => {
  isNative.mockReturnValue(false)
  browserOpen.mockClear()
  vi.spyOn(window, 'open').mockReturnValue(null).mockClear()
})

describe('openExternalUrl', () => {
  it('opens http(s) in a new tab on web', async () => {
    await openExternalUrl('https://example.com/x')
    expect(window.open).toHaveBeenCalledWith('https://example.com/x', '_blank', 'noopener,noreferrer')
  })

  it('refuses non-http(s) schemes', async () => {
    await openExternalUrl('javascript:alert(1)')
    await openExternalUrl('file:///etc/passwd')
    expect(window.open).not.toHaveBeenCalled()
    expect(browserOpen).not.toHaveBeenCalled()
  })

  it('opens a Custom Tab on native', async () => {
    isNative.mockReturnValue(true)
    await openExternalUrl('https://example.com/x')
    expect(browserOpen).toHaveBeenCalledWith({ url: 'https://example.com/x' })
    expect(window.open).not.toHaveBeenCalled()
  })
})

describe('openTerminalLink', () => {
  it('ignores an unmodified click on desktop (TUI input, not activation)', () => {
    openTerminalLink(new MouseEvent('click'), 'https://example.com')
    expect(window.open).not.toHaveBeenCalled()
  })

  it('opens on Ctrl+click and Cmd+click', () => {
    openTerminalLink(new MouseEvent('click', { ctrlKey: true }), 'https://example.com')
    openTerminalLink(new MouseEvent('click', { metaKey: true }), 'https://example.com')
    expect(window.open).toHaveBeenCalledTimes(2)
  })

  it('opens on a plain tap on native', async () => {
    isNative.mockReturnValue(true)
    openTerminalLink(new MouseEvent('click'), 'https://example.com')
    await vi.waitFor(() => expect(browserOpen).toHaveBeenCalledWith({ url: 'https://example.com' }))
  })
})

describe('validateBridgedUrl', () => {
  it('accepts absolute external http(s) and returns the origin', () => {
    const r = validateBridgedUrl('https://docs.example.com/a?b=1')
    expect(r).toEqual({ url: 'https://docs.example.com/a?b=1', origin: 'https://docs.example.com' })
  })

  it('rejects non-strings, relative paths and non-http schemes', () => {
    expect('error' in validateBridgedUrl(42)).toBe(true)
    expect('error' in validateBridgedUrl('/settings')).toBe(true)
    expect('error' in validateBridgedUrl('javascript:alert(1)')).toBe(true)
    expect('error' in validateBridgedUrl('data:text/html,x')).toBe(true)
  })

  it('rejects same-origin URLs (cookie-carrying top-level GET)', () => {
    const r = validateBridgedUrl(`${window.location.origin}/v1/agents`)
    expect(r).toEqual({ error: 'platform links cannot be opened from generated content' })
  })
})
