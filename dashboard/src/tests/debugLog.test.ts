import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The flag is read once when the module loads, so each case loads it fresh
// at the URL it needs.
async function loadAt(url: string) {
  window.history.replaceState({}, '', url)
  vi.resetModules()
  return import('@/lib/debug/log')
}

describe('lib/debug/log', () => {
  beforeEach(() => {
    sessionStorage.clear()
    vi.spyOn(console, 'log').mockImplementation(() => {})
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
    window.history.replaceState({}, '', '/')
  })

  it('prints nothing in a production build without the flag', async () => {
    vi.stubEnv('DEV', false)
    const log = await loadAt('/agents')
    log.debugLog('[wake] listening')
    log.debugWarn('[ws] frame type not in the catalogue')
    expect(log.isDebugOn()).toBe(false)
    expect(console.log).not.toHaveBeenCalled()
    expect(console.warn).not.toHaveBeenCalled()
  })

  it('prints in a development build', async () => {
    vi.stubEnv('DEV', true)
    const log = await loadAt('/agents')
    log.debugLog('[wake] listening')
    expect(console.log).toHaveBeenCalledWith('[wake] listening')
  })

  it('?debug=1 on any page switches the breadcrumbs on for the tab', async () => {
    vi.stubEnv('DEV', false)
    const log = await loadAt('/chat/alpha?debug=1')
    expect(sessionStorage.getItem('otodock.debug')).toBe('1')
    log.debugWarn('[presence] paint failed')
    expect(console.warn).toHaveBeenCalledWith('[presence] paint failed')

    // A later page without the parameter keeps the flag.
    const again = await loadAt('/agents')
    again.debugLog('[build] new build')
    expect(console.log).toHaveBeenCalledWith('[build] new build')
  })

  it('?debug=0 clears the flag', async () => {
    vi.stubEnv('DEV', false)
    sessionStorage.setItem('otodock.debug', '1')
    const log = await loadAt('/agents?debug=0')
    expect(sessionStorage.getItem('otodock.debug')).toBeNull()
    log.debugLog('[map] quality tier lowered')
    expect(console.log).not.toHaveBeenCalled()
  })

  it('logError always prints', async () => {
    vi.stubEnv('DEV', false)
    const log = await loadAt('/agents')
    log.logError('[ws] frame handler failed:', 'chat_history', 'boom')
    expect(console.error).toHaveBeenCalledWith('[ws] frame handler failed:', 'chat_history', 'boom')
  })
})
