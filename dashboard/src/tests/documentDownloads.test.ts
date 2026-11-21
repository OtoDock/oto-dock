import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { fireEvent, render, waitFor } from '@testing-library/react'
import {
  DOWNLOAD_NOT_STARTED, FILE_GONE, checkedDownload, failureSentence, offeredConversions, withName,
} from '@/components/chat/media/documentDownloads'
import DisplayFile from '@/components/chat/media/DisplayFile'
import { ApiError } from '@/lib/http/apiJson'
import { onAppToast, type AppToast } from '@/lib/appToasts'

// ─── The document pane's downloads: what the menu offers and says. ──────────

describe('the pane downloads', () => {
  it('offers the conversions the listing names, in the menu\'s order', () => {
    expect(offeredConversions(['csv', 'pdf'])).toEqual(['pdf', 'csv'])
    expect(offeredConversions(['pdf', 'markdown'])).toEqual(['pdf'])
    expect(offeredConversions([])).toEqual([])
    expect(offeredConversions(undefined)).toEqual([])
  })

  it('names a link for the phone apps', () => {
    expect(withName('/v1/media/t?download=1', 'Αναφορά 1.pdf')).toBe('/v1/media/t?download=1&fn=%CE%91%CE%BD%CE%B1%CF%86%CE%BF%CF%81%CE%AC%201.pdf')
    expect(withName('/v1/media/t', 'a&b.csv')).toBe('/v1/media/t?fn=a%26b.csv')
  })

  it('says the server\'s sentence, else its own', () => {
    expect(failureSentence(new ApiError(413, 'This file is too large to convert here.', null, null), 'pdf'))
      .toBe('This file is too large to convert here.')
    expect(failureSentence(null, 'csv')).toBe('The CSV could not be made. Try again in a moment.')
    expect(failureSentence(undefined, '')).toBe('The download could not be prepared. Try again in a moment.')
  })
})

// ─── A file card's download: its link is asked first. ───────────────────────
// The Android app's web view drops a download whose answer is an error and
// tells nobody, so a card that cannot download must say why itself.

// secrets.token_urlsafe(32): what a card's media link carries.
const TOK = 'A'.repeat(20) + 'b_-'.repeat(7) + 'z'.repeat(2)
const CARD = `/v1/media/${TOK}?download=1&fn=report.pdf`

function answer(status: number, body: unknown = null) {
  return new Response(body === null ? '' : JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  })
}

describe('a file card download', () => {
  let started: string[]
  let fetched: Array<{ url: string; range: string | null }>

  beforeEach(() => {
    started = []
    fetched = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      started.push(`${this.getAttribute('href')} as ${this.download}`)
    })
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  function serve(res: () => Response | Promise<Response>) {
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      fetched.push({ url, range: new Headers(init?.headers).get('Range') })
      return res()
    }))
  }

  it('asks for the first byte with the sign-in, then starts the download', async () => {
    serve(() => new Response('%', { status: 206 }))
    expect(await checkedDownload(CARD, 'report.pdf')).toBe('')
    expect(fetched).toEqual([{ url: CARD, range: 'bytes=0-0' }])
    expect(started).toEqual([`${CARD} as report.pdf`])
  })

  it('says the file is gone and starts nothing when the link answers 404', async () => {
    serve(() => answer(404, { detail: 'media file no longer exists' }))
    expect(await checkedDownload(CARD, 'report.pdf')).toBe(FILE_GONE)
    expect(started).toEqual([])
  })

  it('says the server\'s sentence when the machine that holds the file is away', async () => {
    serve(() => answer(503, { detail: 'This file lives on the remote machine, which is offline. Reconnect it and try again.' }))
    expect(await checkedDownload(CARD, 'report.pdf'))
      .toBe('This file lives on the remote machine, which is offline. Reconnect it and try again.')
    expect(started).toEqual([])
  })

  it('says the download did not start when nothing answers', async () => {
    serve(() => { throw new TypeError('Failed to fetch') })
    expect(await checkedDownload(CARD, 'report.pdf')).toBe(DOWNLOAD_NOT_STARTED)
    serve(() => answer(500))
    expect(await checkedDownload(CARD, 'report.pdf')).toBe(DOWNLOAD_NOT_STARTED)
    expect(started).toEqual([])
  })

  it('a tap on a card whose file is gone says why on the card, with no notification', async () => {
    serve(() => answer(404, { detail: 'media file no longer exists' }))
    const toasts: AppToast[] = []
    const off = onAppToast((t) => toasts.push(t))
    const { getByRole, findByRole } = render(createElement(DisplayFile, { filename: 'report.pdf', downloadUrl: `/v1/media/${TOK}?download=1` }))
    const card = getByRole('link')
    expect(card.getAttribute('href')).toBe(CARD)
    expect(fireEvent.click(card)).toBe(false)
    const line = await findByRole('alert')
    off()
    expect(line.textContent).toBe(FILE_GONE)
    expect(line.closest('[data-file-card]')).not.toBeNull()
    expect(toasts).toEqual([])
    expect(started).toEqual([])
  })

  it('the card\'s line stays until a later try starts the download', async () => {
    serve(() => answer(404, { detail: 'media file no longer exists' }))
    const { getByRole, findByRole, queryByRole } = render(createElement(DisplayFile, { filename: 'report.pdf', downloadUrl: `/v1/media/${TOK}?download=1` }))
    fireEvent.click(getByRole('link'))
    await findByRole('alert')
    serve(() => answer(500))
    fireEvent.click(getByRole('link'))
    await waitFor(() => expect(getByRole('alert').textContent).toBe(DOWNLOAD_NOT_STARTED))
    serve(() => new Response('%', { status: 206 }))
    fireEvent.click(getByRole('link'))
    await waitFor(() => expect(queryByRole('alert')).toBeNull())
    expect(started).toEqual([`${CARD} as report.pdf`])
  })

  it('asking the link itself raises no notification', async () => {
    serve(() => answer(404, { detail: 'media file no longer exists' }))
    const toasts: AppToast[] = []
    const off = onAppToast((t) => toasts.push(t))
    expect(await checkedDownload(CARD, 'report.pdf')).toBe(FILE_GONE)
    off()
    expect(toasts).toEqual([])
  })

  it('a tap on a card that answers starts its download once', async () => {
    serve(() => new Response('%', { status: 206 }))
    const { getByRole } = render(createElement(DisplayFile, { filename: 'report.pdf', downloadUrl: `/v1/media/${TOK}?download=1` }))
    fireEvent.click(getByRole('link'))
    fireEvent.click(getByRole('link'))
    await waitFor(() => expect(started).toEqual([`${CARD} as report.pdf`]))
    expect(fetched).toHaveLength(1)
  })

  it('leaves a shared chat\'s copy to the browser', () => {
    serve(() => new Response('%', { status: 206 }))
    const { getByRole } = render(createElement(DisplayFile, { filename: 'report.pdf', downloadUrl: '/v1/shares/s1/media/abc' }))
    expect(fireEvent.click(getByRole('link'))).toBe(true)
    expect(fetched).toEqual([])
  })

  it('leaves a click with a key held to the browser', () => {
    serve(() => new Response('%', { status: 206 }))
    const { getByRole } = render(createElement(DisplayFile, { filename: 'report.pdf', downloadUrl: `/v1/media/${TOK}?download=1` }))
    expect(fireEvent.click(getByRole('link'), { ctrlKey: true })).toBe(true)
    expect(fetched).toEqual([])
  })
})
