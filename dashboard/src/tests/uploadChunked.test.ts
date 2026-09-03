// uploadWithProgress chunked path: threshold switch, slicing by the SERVER's
// returned chunk size, sequential PUTs with aggregate progress, per-chunk
// retry on transient failures, no retry on 4xx, abort → DELETE cleanup.
// jsdom has an XHR class but no network — the stub below is the seam.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { uploadWithProgress, UPLOAD_CHUNK_THRESHOLD } from '@/lib/uploadWithProgress'

class FakeXHR {
  static instances: FakeXHR[] = []
  static autoRespond: ((x: FakeXHR) => void) | null = null
  method = ''
  url = ''
  status = 0
  response: unknown = null
  responseType = ''
  withCredentials = false
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null
  sentBody: unknown = null
  open(method: string, url: string) { this.method = method; this.url = url }
  send(body: unknown) {
    this.sentBody = body
    FakeXHR.instances.push(this)
    queueMicrotask(() => FakeXHR.autoRespond?.(this))
  }
  abort() { this.onabort?.() }
  respond(status: number, response: unknown) {
    this.status = status
    this.response = response
    this.onload?.()
  }
}

const fetchCalls: Array<{ url: string; init: RequestInit | undefined }> = []
let fetchResponder: (url: string, init?: RequestInit) => { status: number; body: unknown }

function fetchMock(url: string, init?: RequestInit) {
  fetchCalls.push({ url: String(url), init })
  const { status, body } = fetchResponder(String(url), init)
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

const BIG = UPLOAD_CHUNK_THRESHOLD + 5 // just over the threshold
// Server dictates a SMALL chunk size (nothing says it must equal the client
// threshold) — proves slicing follows the response, not the local constant.
const SRV_CHUNK = 8 * 1024 * 1024

function bigFile(name = 'big.bin'): File {
  return new File([new Uint8Array(BIG)], name)
}

function okResponder(url: string): { status: number; body: unknown } {
  if (url.endsWith('/init')) {
    return { status: 200, body: { upload_id: 'up_test123', chunk_size: SRV_CHUNK } }
  }
  if (url.endsWith('/complete')) {
    return {
      status: 200,
      body: { path: 'p/big.bin', filename: 'big.bin', size: BIG, transfer_id: 't1', remote_push: false },
    }
  }
  return { status: 200, body: { ok: true } }
}

describe('uploadWithProgress — chunked path', () => {
  beforeEach(() => {
    FakeXHR.instances = []
    FakeXHR.autoRespond = null
    fetchCalls.length = 0
    fetchResponder = okResponder
    vi.stubGlobal('XMLHttpRequest', FakeXHR as unknown as typeof XMLHttpRequest)
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('small files keep the single multipart POST (no chunk endpoints touched)', async () => {
    FakeXHR.autoRespond = (x) => x.respond(200, {
      path: 'p/s.bin', filename: 's.bin', size: 10, transfer_id: 't', remote_push: false,
    })
    const resp = await uploadWithProgress(
      new File([new Uint8Array(10)], 's.bin'), 'ag', '', () => {},
    )
    expect(resp.path).toBe('p/s.bin')
    expect(FakeXHR.instances).toHaveLength(1)
    expect(FakeXHR.instances[0].url).toBe('/v1/upload')
    expect(FakeXHR.instances[0].sentBody).toBeInstanceOf(FormData)
    expect(fetchCalls).toHaveLength(0)
  })

  it('slices by the server-returned chunk size, sequentially, with aggregate progress', async () => {
    FakeXHR.autoRespond = (x) => {
      // Mid-chunk progress before the 2xx — aggregate must be monotonic.
      x.upload.onprogress?.({ lengthComputable: true, loaded: 1000, total: SRV_CHUNK })
      x.respond(200, { ok: true })
    }
    const progress: Array<[number, number]> = []
    const resp = await uploadWithProgress(
      bigFile(), 'ag', '', (sent, total) => progress.push([sent, total]),
    )
    expect(resp.transfer_id).toBe('t1')
    const expectedChunks = Math.ceil(BIG / SRV_CHUNK)
    expect(FakeXHR.instances).toHaveLength(expectedChunks)
    FakeXHR.instances.forEach((x, i) => {
      expect(x.method).toBe('PUT')
      expect(x.url).toBe(`/v1/upload/chunked/up_test123/${i}`)
      const blob = x.sentBody as Blob
      const expectedSize = Math.min(SRV_CHUNK, BIG - i * SRV_CHUNK)
      expect(blob.size).toBe(expectedSize)
    })
    // init + complete rode fetch
    expect(fetchCalls.map(c => c.url)).toEqual([
      '/v1/upload/chunked/init',
      '/v1/upload/chunked/up_test123/complete',
    ])
    // Progress: totals always file.size; sent monotonic; ends complete.
    expect(progress.every(([, t]) => t === BIG)).toBe(true)
    for (let i = 1; i < progress.length; i++) {
      expect(progress[i][0]).toBeGreaterThanOrEqual(progress[i - 1][0])
    }
    expect(progress[progress.length - 1][0]).toBe(BIG)
  })

  it('retries a transiently-failed chunk without restarting the upload', async () => {
    let failedOnce = false
    FakeXHR.autoRespond = (x) => {
      if (x.url.endsWith('/0') && !failedOnce) {
        failedOnce = true
        x.onerror?.()
        return
      }
      x.respond(200, { ok: true })
    }
    const resp = await uploadWithProgress(bigFile(), 'ag', '', () => {})
    expect(resp.path).toBe('p/big.bin')
    const chunk0Attempts = FakeXHR.instances.filter(x => x.url.endsWith('/0'))
    expect(chunk0Attempts).toHaveLength(2)
  }, 15000)

  it('does not retry a 4xx chunk failure', async () => {
    FakeXHR.autoRespond = (x) => x.respond(413, { detail: 'File too large (max 1024 MB)' })
    await expect(
      uploadWithProgress(bigFile(), 'ag', '', () => {}),
    ).rejects.toThrow('File too large (max 1024 MB)')
    expect(FakeXHR.instances).toHaveLength(1)
  })

  it('abort mid-chunk rejects AbortError and fires the DELETE cleanup', async () => {
    const controller = new AbortController()
    FakeXHR.autoRespond = (x) => {
      if (x.url.endsWith('/0')) {
        controller.abort() // the abort listener calls xhr.abort() → onabort
        return
      }
      x.respond(200, { ok: true })
    }
    await expect(
      uploadWithProgress(bigFile(), 'ag', '', () => {}, controller.signal),
    ).rejects.toMatchObject({ name: 'AbortError' })
    const del = fetchCalls.find(c => c.init?.method === 'DELETE')
    expect(del?.url).toBe('/v1/upload/chunked/up_test123')
  })
})
