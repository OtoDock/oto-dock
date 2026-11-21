// A file card the display tool drew, dragged to another chat's message box.
//
// The card writes a reference in a type of our own: the media token of its
// download (`/v1/media/<token>?download=1`, the route that serves the file to
// whoever may read the chat), its name, and the page's origin. The drop is
// untrusted: any page can start a drag that carries this type. So the drop
// takes the reference in one fixed shape, builds the address itself on its
// own origin, and fetches with the person's own sign-in. The platform's
// answer decides who may read the file (another person's file is a 404).
// The origin in the reference only gives an honest drag from another
// install a kinder sentence than that 404: a page can write any origin.

import { getFileExtension } from '../fileTypes'
import { apiResponse } from '../http/apiJson'

export const FILE_DRAG_TYPE = 'application/x-otodock-file'

/** The reference a card carries. */
export interface FileDragRef {
  v: 1
  origin: string
  token: string
  name: string
}

// secrets.token_urlsafe(32): 43 characters of the URL-safe alphabet.
const TOKEN_RE = /^[A-Za-z0-9_-]{43}$/
const CARD_URL_RE = /^\/v1\/media\/([A-Za-z0-9_-]{43})(?:\?.*)?$/
const NAME_MAX = 255
// Path separators and control characters never belong in a file's name.
// eslint-disable-next-line no-control-regex
const NAME_BAD_RE = /[/\\\u0000-\u001f\u007f]/
const ONLY_DOTS_RE = /^\.+$/
const RAW_MAX = 2048
const KEYS = ['name', 'origin', 'token', 'v']

export const REFUSED_OTHER_INSTALL = 'This file is from another OtoDock.'
export const REFUSED_SHAPE = 'This file cannot be attached.'
export const REFUSED_NOT_YOURS = 'You cannot open this file.'
export const REFUSED_NO_ANSWER = 'The file could not be fetched.'

function mediaUrl(token: string): string {
  return `/v1/media/${token}?download=1`
}

/** The card's reference, or null when its address is not a media download
 *  (a shared chat's copy, an empty address). */
export function fileDragRef(downloadUrl: string, name: string): FileDragRef | null {
  const m = CARD_URL_RE.exec(downloadUrl || '')
  if (!m || !name) return null
  return { v: 1, origin: window.location.origin, token: m[1], name }
}

/** Whether this browser uses a drag's `DownloadURL` line and keeps it from
 *  the page the drag is dropped on. Chromium (Chrome, Edge) saves the file on
 *  a drop on the desktop and never shows the line to a page. Firefox does not
 *  use the line and hands it to any page as plain data, the card's address
 *  in it, and Safari does not use it. The client hints answer where the
 *  browser gives them (a secure page), the user agent otherwise. A browser
 *  that cannot be told is answered no. */
export function keepsDownloadUrl(): boolean {
  const hints = (navigator as Navigator & { userAgentData?: { brands?: Array<{ brand?: string }> } }).userAgentData
  if (hints && Array.isArray(hints.brands) && hints.brands.length) {
    return hints.brands.some((b) => b?.brand === 'Chromium')
  }
  const ua = navigator.userAgent || ''
  return /\b(?:Chrome|Chromium)\/\d/.test(ua) && !/\b(?:Firefox|FxiOS)\//.test(ua)
}

/** Fill a drag that starts on a card: our type, and where the browser keeps
 *  it to itself the download a drop on the desktop saves (`href`, the card's
 *  own link). The link's own data (its address as text) is cleared, so no
 *  text field elsewhere takes the address. */
export function writeFileDrag(dt: DataTransfer, ref: FileDragRef, href: string): void {
  dt.clearData()
  dt.effectAllowed = 'copy'
  dt.setData(FILE_DRAG_TYPE, JSON.stringify(ref))
  if (!keepsDownloadUrl()) return
  const absolute = new URL(href, window.location.origin).href
  // `mime:name:url`: a colon in the name would end the name early.
  dt.setData('DownloadURL', `application/octet-stream:${ref.name.replace(/:/g, '_')}:${absolute}`)
}

/** Whether a drag carries a card. Only the types are readable during a drag. */
export function carriesFileDrag(dt: DataTransfer | null | undefined): boolean {
  return Array.from(dt?.types ?? []).includes(FILE_DRAG_TYPE)
}

/** A drag the composer takes: files from the computer, or a card. */
export function composerTakesDrag(dt: DataTransfer | null | undefined): boolean {
  return Array.from(dt?.types ?? []).includes('Files') || carriesFileDrag(dt)
}

/** The checked reference a drop carries, a refusal sentence, or null when
 *  it carries no card. */
export function readFileDrag(dt: DataTransfer | null | undefined): FileDragRef | { refused: string } | null {
  if (!dt || !carriesFileDrag(dt)) return null
  const raw = dt.getData(FILE_DRAG_TYPE)
  if (!raw || raw.length > RAW_MAX) return { refused: REFUSED_SHAPE }
  let parsed: unknown
  try { parsed = JSON.parse(raw) } catch { return { refused: REFUSED_SHAPE } }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return { refused: REFUSED_SHAPE }
  const o = parsed as Record<string, unknown>
  if (Object.keys(o).sort().join(',') !== KEYS.join(',') || o.v !== 1) return { refused: REFUSED_SHAPE }
  if (typeof o.origin !== 'string' || typeof o.token !== 'string' || typeof o.name !== 'string') {
    return { refused: REFUSED_SHAPE }
  }
  if (o.origin !== window.location.origin) return { refused: REFUSED_OTHER_INSTALL }
  const name = o.name
  if (!TOKEN_RE.test(o.token) || !name || name.length > NAME_MAX || NAME_BAD_RE.test(name)
      || ONLY_DOTS_RE.test(name)) {
    return { refused: REFUSED_SHAPE }
  }
  return { v: 1, origin: o.origin, token: o.token, name }
}

/** The name a `Content-Disposition` header gives, either form, or ''. */
export function dispositionName(header: string | null): string {
  if (!header) return ''
  const star = /filename\*\s*=\s*(?:utf-8|UTF-8)''([^;]+)/.exec(header)
  if (star) {
    try { return decodeURIComponent(star[1].trim()) } catch { /* the plain form below */ }
  }
  const plain = /filename\s*=\s*"((?:[^"\\]|\\.)*)"|filename\s*=\s*([^;]+)/.exec(header)
  if (!plain) return ''
  return (plain[1] ?? plain[2] ?? '').replace(/\\(.)/g, '$1').trim()
}

/** The attached file's name: the card's when it keeps the server's file
 *  type, else the server's own (a forged name cannot relabel a file). */
export function pickName(refName: string, served: string): string {
  if (!served) return refName
  return getFileExtension(refName) === getFileExtension(served) ? refName : served
}

/** Thrown with the sentence the chip shows. */
export class DropRefused extends Error {}

/** Fetch a dropped card's file with the person's own sign-in, refusing past
 *  the upload cap before more than the cap is read. */
export async function fetchDroppedFile(ref: FileDragRef, cap: number, signal: AbortSignal,
                                       overCap: (cap: number) => string): Promise<File> {
  let res: Response
  try {
    res = await apiResponse(mediaUrl(ref.token), { signal })
  } catch (e) {
    if (signal.aborted) throw e
    throw new DropRefused(REFUSED_NO_ANSWER)
  }
  if (res.status === 401 || res.status === 403 || res.status === 404) throw new DropRefused(REFUSED_NOT_YOURS)
  if (!res.ok) throw new DropRefused(REFUSED_NO_ANSWER)
  const length = Number(res.headers.get('content-length') || NaN)
  if (Number.isFinite(length) && length > cap) {
    void res.body?.cancel().catch(() => {})
    throw new DropRefused(overCap(cap))
  }
  const type = (res.headers.get('content-type') || '').split(';')[0].trim()
  const name = pickName(ref.name, dispositionName(res.headers.get('content-disposition')))
  const parts: BlobPart[] = []
  let read = 0
  if (res.body) {
    const reader = res.body.getReader()
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      read += value.byteLength
      if (read > cap) {
        void reader.cancel().catch(() => {})
        throw new DropRefused(overCap(cap))
      }
      parts.push(value)
    }
  } else {
    const blob = await res.blob()
    if (blob.size > cap) throw new DropRefused(overCap(cap))
    parts.push(blob)
  }
  return new File(parts, name, { type })
}
