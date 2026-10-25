import { errorName } from './http/apiJson'
import { isRecord } from './json/guards'
// XHR-based upload with byte progress. fetch() has no upload-progress events
// (short of experimental duplex streams), so the workspace/chat upload paths
// use XMLHttpRequest when they want a progress bar. Auth is the same-origin
// session cookie (mirrors apiFetch: no headers needed, 401 → back to login).
//
// Files bigger than one chunk upload CHUNKED: init → sequential raw-body PUTs
// → complete. A CDN/gateway request-body
// cap (Cloudflare ~100MB, nginx client_max_body_size) then never sees the
// whole file, and a network blip retries ONE chunk instead of restarting a
// 300MB upload. The server DICTATES the chunk size via init's response — the
// local constant only decides single-shot vs chunked. Both entrances (chat
// composer queue, workspace overlay) call this one function, so the switch is
// invisible to callers: same signature, same UploadResponse, same abort
// semantics (AbortError DOMException), aggregate progress with
// total = file.size.

export interface UploadResponse {
  path: string
  filename: string
  size: number
  transfer_id: string
  remote_push: boolean
}

// Matches the server's shipped OTODOCK_UPLOAD_CHUNK_MB default. Only the
// single-shot/chunked DECISION uses it — slicing follows init's returned
// chunk_size (an operator-raised server knob just works).
export const UPLOAD_CHUNK_THRESHOLD = 32 * 1024 * 1024

// Transient-failure retries per chunk (network error / 5xx), with short
// backoff. 4xx (cap, auth, gone) surface immediately.
const CHUNK_RETRIES = 3
const CHUNK_RETRY_DELAYS_MS = [500, 1500]

export function uploadWithProgress(
  file: File,
  agent: string,
  targetDir: string,
  onProgress: (sent: number, total: number) => void,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  if (file.size <= UPLOAD_CHUNK_THRESHOLD) {
    return uploadSingle(file, agent, targetDir, onProgress, signal)
  }
  return uploadChunked(file, agent, targetDir, onProgress, signal)
}

function uploadSingle(
  file: File,
  agent: string,
  targetDir: string,
  onProgress: (sent: number, total: number) => void,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    // Abort rejects with a DOMException named AbortError — same shape as an
    // aborted fetch(), so callers share one "user cancelled" check.
    if (signal?.aborted) {
      reject(new DOMException('Upload aborted', 'AbortError'))
      return
    }
    const xhr = new XMLHttpRequest()
    signal?.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.open('POST', '/v1/upload')
    xhr.withCredentials = false // same-origin — cookies ride automatically
    xhr.responseType = 'json'
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded, e.total)
    }
    xhr.onload = () => {
      if (xhr.status === 401) {
        window.location.href = '/'
        reject(new Error('Unauthorized'))
        return
      }
      if (xhr.status >= 200 && xhr.status < 300 && xhr.response) {
        resolve(xhr.response as UploadResponse)
      } else {
        const body: unknown = xhr.response
        const detail = (isRecord(body) && body.detail) || (xhr.status === 413 ? 'The file is too large for the server.' : 'The upload failed. Try again in a minute.')
        reject(new Error(String(detail)))
      }
    }
    xhr.onerror = () => {
      const err = new Error('Network error during upload') as Error & { transient?: boolean }
      err.transient = true
      reject(err)
    }
    xhr.onabort = () => reject(new DOMException('Upload aborted', 'AbortError'))
    const fd = new FormData()
    // The name given explicitly: a folder-picked file's multipart name is
    // otherwise its webkitRelativePath (`folder/sub/x.md`), which the server
    // flattens into one file name.
    fd.append('file', file, file.name)
    fd.append('agent', agent)
    fd.append('target_dir', targetDir)
    xhr.send(fd)
  })
}

// --- chunked path -----------------------------------------------------------

async function jsonRequest<T>(
  method: string,
  url: string,
  body: unknown | undefined,
  signal?: AbortSignal,
): Promise<T> {
  const resp = await fetch(url, {
    method,
    credentials: 'same-origin',
    signal,
    ...(body !== undefined
      ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
      : {}),
  })
  if (resp.status === 401) {
    window.location.href = '/'
    throw new Error('Unauthorized')
  }
  if (!resp.ok) {
    let detail = 'The server did not accept the request. Try again in a minute.'
    try {
      const data = await resp.json()
      if (data?.detail) detail = String(data.detail)
    } catch { /* non-JSON error body (edge/gateway) — keep the fallback sentence */ }
    throw new Error(detail)
  }
  return resp.json()
}

function putChunk(
  uploadId: string,
  index: number,
  blob: Blob,
  onProgress: (sent: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Upload aborted', 'AbortError'))
      return
    }
    const xhr = new XMLHttpRequest()
    const onAbort = () => xhr.abort()
    signal?.addEventListener('abort', onAbort, { once: true })
    const cleanup = () => signal?.removeEventListener('abort', onAbort)
    xhr.open('PUT', `/v1/upload/chunked/${uploadId}/${index}`)
    xhr.withCredentials = false
    xhr.responseType = 'json'
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded)
    }
    xhr.onload = () => {
      cleanup()
      if (xhr.status === 401) {
        window.location.href = '/'
        reject(new Error('Unauthorized'))
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve()
        return
      }
      const body: unknown = xhr.response
      const detail = (isRecord(body) && body.detail) || (xhr.status === 413 ? 'The file is too large for the server.' : 'The upload failed. Try again in a minute.')
      const err = new Error(String(detail)) as Error & { status?: number }
      err.status = xhr.status
      reject(err)
    }
    xhr.onerror = () => {
      cleanup()
      const err = new Error('Network error during upload') as Error & { transient?: boolean }
      err.transient = true
      reject(err)
    }
    xhr.onabort = () => {
      cleanup()
      reject(new DOMException('Upload aborted', 'AbortError'))
    }
    xhr.send(blob)
  })
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Upload aborted', 'AbortError'))
      return
    }
    const t = setTimeout(() => { cleanup(); resolve() }, ms)
    const onAbort = () => {
      clearTimeout(t)
      cleanup()
      reject(new DOMException('Upload aborted', 'AbortError'))
    }
    const cleanup = () => signal?.removeEventListener('abort', onAbort)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

async function putChunkWithRetry(
  uploadId: string,
  index: number,
  blob: Blob,
  onProgress: (sent: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  let lastErr: unknown
  for (let attempt = 0; attempt < CHUNK_RETRIES; attempt++) {
    if (attempt > 0) {
      await delay(CHUNK_RETRY_DELAYS_MS[attempt - 1] ?? 1500, signal)
      onProgress(0) // reset this chunk's visible progress for the re-send
    }
    try {
      await putChunk(uploadId, index, blob, onProgress, signal)
      return
    } catch (e) {
      if (errorName(e) === 'AbortError') throw e
      const failure = isRecord(e) ? e : {}
      const retriable = failure.transient === true ||
        (typeof failure.status === 'number' && failure.status >= 500)
      if (!retriable) throw e
      lastErr = e
    }
  }
  throw lastErr
}

async function uploadChunked(
  file: File,
  agent: string,
  targetDir: string,
  onProgress: (sent: number, total: number) => void,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  const init = await jsonRequest<{ upload_id: unknown; chunk_size: unknown }>('POST', '/v1/upload/chunked/init', {
    agent,
    target_dir: targetDir,
    filename: file.name,
    size: file.size,
  }, signal)
  const uploadId = String(init.upload_id)
  const chunkSize = Number(init.chunk_size) // authoritative — slice by THIS
  const nChunks = Math.ceil(file.size / chunkSize)
  try {
    for (let i = 0; i < nChunks; i++) {
      const base = i * chunkSize
      const blob = file.slice(base, Math.min(base + chunkSize, file.size))
      await putChunkWithRetry(
        uploadId, i, blob,
        // Clamp: aggregate progress must never overshoot the file size even
        // if an XHR reports a padded/odd e.loaded for the (smaller) last chunk.
        (sent) => onProgress(Math.min(base + sent, file.size), file.size),
        signal,
      )
      onProgress(Math.min(base + blob.size, file.size), file.size)
    }
    const done = await jsonRequest<UploadResponse>(
      'POST', `/v1/upload/chunked/${uploadId}/complete`, {}, signal,
    )
    onProgress(file.size, file.size)
    return done
  } catch (e) {
    // Every error reaching here is terminal for this upload (an abort, a
    // refused chunk or complete, exhausted retries): release the staging
    // now rather than holding a slot of the caller's cap until the sweep.
    // Best-effort; keepalive survives page teardown.
    void fetch(`/v1/upload/chunked/${uploadId}`, {
      method: 'DELETE', credentials: 'same-origin', keepalive: true,
    }).catch(() => {})
    throw e
  }
}

/** The upload never reached the server (no answer at all), as opposed to
 * a refusal: what a dropped connection looks like to every request here. */
export function isNetworkError(e: unknown): boolean {
  return e instanceof TypeError || (e as { transient?: boolean } | null)?.transient === true
}

/** A fresh folder for a folder upload, named after it (`_1`, `_2`… when
 * taken): in `targetDir`, or the chat's own upload folder when it is empty.
 * Its files then upload into it with an explicit target dir. */
export async function reserveUploadFolder(
  agent: string,
  name: string,
  signal?: AbortSignal,
  targetDir = '',
): Promise<{ path: string; name: string }> {
  return jsonRequest<{ path: string; name: string }>('POST', '/v1/upload/folder', { agent, name, target_dir: targetDir }, signal)
}
