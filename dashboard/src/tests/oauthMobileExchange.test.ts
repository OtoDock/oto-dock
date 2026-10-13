/**
 * The app's half of an MCP account connect: the otodock:// link brings the
 * vendor's code and the state back to the app, and the exchange runs under
 * the app's own login; a link without a code (an older server exchanged on
 * the callback) does nothing, and a refusal surfaces the server's words.
 * A link that arrives with no waiter (the page was recreated while the
 * person consented) is completed by the handler itself: the exchange, the
 * accounts refreshed, a toast; a pending waiter takes the link and nothing
 * else happens.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as authApi from '@/api/auth'
import { exchangeOAuthCode } from '@/api/oauth'
import { onAppToast, type AppToast } from '@/lib/appToasts'
import * as oauth from '@/lib/oauth'
import { queryClient } from '@/lib/queryClient'

vi.mock('@capacitor/core', () => ({ Capacitor: { isNativePlatform: () => true } }))

const fetchSpy = vi.spyOn(authApi, 'apiFetch')
const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries').mockImplementation(async () => {})
afterEach(() => { fetchSpy.mockReset(); invalidateSpy.mockClear() })

const LINK = 'otodock://oauth/google/complete?code=c-1&state=s-1&iss=https%3A%2F%2Fid.example&install=i-1'

describe('exchangeOAuthCode', () => {
  it('posts the code, the state and the issuer from the link', async () => {
    fetchSpy.mockResolvedValue(new Response(JSON.stringify({ status: 'ok' }), { status: 200 }))
    await exchangeOAuthCode('google', LINK)
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    const [url, init] = fetchSpy.mock.calls[0]
    expect(url).toBe('/v1/oauth/google/exchange')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(init?.body as string)).toEqual({
      code: 'c-1', state: 's-1', iss: 'https://id.example',
    })
  })

  it('does nothing for a link without a code', async () => {
    await exchangeOAuthCode('google', 'otodock://oauth/google/complete?email=a%40b.c')
    await exchangeOAuthCode('google', 'not a url')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it("throws the server's refusal", async () => {
    fetchSpy.mockResolvedValue(new Response(
      JSON.stringify({ detail: 'State was issued for a different user' }), { status: 403 },
    ))
    await expect(exchangeOAuthCode('google', LINK))
      .rejects.toThrow('State was issued for a different user')
  })
})

describe('_handleDeepLink with no waiter pending', () => {
  type Handler = (url: string) => Promise<boolean> | undefined
  const handler = () => (window as unknown as { _handleDeepLink: Handler })._handleDeepLink
  const invalidated = () => invalidateSpy.mock.calls.map((c) => c[0]?.queryKey)
  let toasts: AppToast[]
  let stop: () => void

  beforeEach(() => {
    toasts = []
    stop = onAppToast((t) => toasts.push(t))
  })
  afterEach(() => stop())

  it('completes the connect itself: the exchange, the accounts refreshed, a toast', async () => {
    fetchSpy.mockResolvedValue(new Response(JSON.stringify({ status: 'ok' }), { status: 200 }))
    await expect(handler()(LINK)).resolves.toBe(true)
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    expect(fetchSpy.mock.calls[0][0]).toBe('/v1/oauth/google/exchange')
    expect(invalidated()).toEqual([['my-integrations'], ['admin-integrations']])
    expect(toasts).toEqual([{ severity: 'success', title: 'Account connected', body: 'Your Google account is connected.' }])
  })

  it("shows the server's refusal and refreshes nothing", async () => {
    fetchSpy.mockResolvedValue(new Response(
      JSON.stringify({ detail: 'State was issued for a different user' }), { status: 403 },
    ))
    await expect(handler()(LINK)).resolves.toBe(false)
    expect(invalidated()).toEqual([])
    expect(toasts).toEqual([{ severity: 'warning', title: 'Account not connected', body: 'State was issued for a different user' }])
  })

  it('leaves a link without a code, or of another flow, alone', async () => {
    await expect(handler()('otodock://oauth/google/complete?email=a%40b.c')).resolves.toBe(false)
    await expect(handler()('otodock://auth/callback?code=c&state=s')).resolves.toBe(false)
    await expect(handler()('otodock://oauth/microsoft/admin-consent/complete?tenant=t')).resolves.toBe(false)
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(toasts).toEqual([])
    expect(invalidated()).toEqual([])
  })

  it('a pending waiter takes the link and nothing else happens', async () => {
    const link = oauth.waitForDeepLink(60_000)
    expect(handler()(LINK)).toBeUndefined()
    await expect(link).resolves.toBe(LINK)
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(toasts).toEqual([])
    expect(invalidated()).toEqual([])
  })
})
