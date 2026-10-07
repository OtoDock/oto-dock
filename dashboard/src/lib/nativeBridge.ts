/**
 * The Android app's native bridge. The app injects `OtoDockNative` (a WebView
 * message listener) only into frames of the installation it is showing, and
 * honours a message only when it is posted on the top-level document's
 * object: the boundary is same-origin script (a same-origin child frame
 * reaches the channel through `top.OtoDockNative`), so a frame that renders
 * third-party content stays off this origin. On the web, and in a sandboxed
 * or other-origin frame, there is no channel and every call here is a no-op.
 *
 * A message is a JSON header `{m, a, id?}`, optionally followed by a newline
 * and a raw body (a saved file's base64, so the app never parses it as JSON).
 * The replying methods answer `{id, r}` on the same object; the app runs the
 * calls one at a time, in the order they were posted.
 *
 * - setNativeAuthInProgress: block an install switch (swipe / Settings) while an
 *   auth flow that holds in-WebView state is running (Claude code-paste, OpenAI
 *   device-code). Deep-link flows arm this natively in openAuthBrowser.
 * - setNativeSwitchBusy: hint that live work (a meeting/voice call, etc.) would be
 *   lost on a switch, so the switcher confirms first. LLM streaming is already
 *   reported via setStreaming.
 */

export type NativeMethod =
  | 'dashboardReady'
  | 'setStreaming'
  | 'setContainerColor'
  | 'enterVideoFullscreen'
  | 'exitVideoFullscreen'
  | 'flushCookies'
  | 'setAuthInProgress'
  | 'setSwitchBusy'
  | 'openAddInstallation'
  | 'switchInstallation'
  | 'removeInstallation'
  | 'setFavorite'
  | 'renameInstallation'
  | 'disconnectServer'

export type NativeQuery = 'getInstallations' | 'switchToInstall' | 'openAuthBrowser'

export type NativeArg = string | number | boolean | null

/** A person may spend a while at the app's "open in your browser?" confirm. */
export const NATIVE_BROWSER_TIMEOUT_MS = 10 * 60 * 1000

interface NativeChannel {
  postMessage(message: string): void
  addEventListener(type: 'message', listener: (event: { data: unknown }) => void): void
}

declare global {
  interface Window {
    OtoDockNative?: NativeChannel
  }
}

function channel(): NativeChannel | null {
  if (typeof window === 'undefined') return null
  const c = window.OtoDockNative
  return c && typeof c.postMessage === 'function' ? c : null
}

/** True inside the Android app, on the page the app scoped its channel to. */
export function hasNativeBridge(): boolean {
  return channel() !== null
}

function post(c: NativeChannel, header: object, body?: string): boolean {
  try {
    // Always ON the injected object: a detached method reference is not bound to it.
    c.postMessage(JSON.stringify(header) + (body === undefined ? '' : '\n' + body))
    return true
  } catch {
    return false
  }
}

/** Fire-and-forget call; false (and nothing done) off the app. */
export function callNative(method: NativeMethod, ...args: NativeArg[]): boolean {
  const c = channel()
  return c ? post(c, { m: method, a: args }) : false
}

let nextId = 0
const waiting = new Map<number, (value: unknown) => void>()
let listeningOn: NativeChannel | null = null

function listen(c: NativeChannel) {
  if (listeningOn === c) return
  listeningOn = c
  c.addEventListener('message', (event) => {
    let reply: { id?: unknown; r?: unknown }
    try {
      reply = typeof event.data === 'string' ? JSON.parse(event.data) : (event.data as object)
    } catch {
      return
    }
    if (typeof reply?.id !== 'number') return
    const resolve = waiting.get(reply.id)
    if (!resolve) return
    waiting.delete(reply.id)
    resolve(reply.r)
  })
}

/** A call the app answers. Resolves undefined off the app (at once) and when no
 *  answer arrives within the timeout. */
export function askNative<T>(
  method: NativeQuery, args: NativeArg[] = [], timeoutMs = 3000,
): Promise<T | undefined> {
  const c = channel()
  if (!c) return Promise.resolve(undefined)
  listen(c)
  const id = ++nextId
  return new Promise<T | undefined>((resolve) => {
    const timer = setTimeout(() => {
      waiting.delete(id)
      resolve(undefined)
    }, timeoutMs)
    waiting.set(id, (value) => {
      clearTimeout(timer)
      resolve(value as T)
    })
    if (!post(c, { m: method, a: args, id })) {
      clearTimeout(timer)
      waiting.delete(id)
      resolve(undefined)
    }
  })
}

/** Save a base64 payload to the phone's Downloads (the app accepts the image
 *  types the lightbox shows and JSON diagnostics). */
export function saveNativeFile(base64: string, name: string, mime: string): boolean {
  const c = channel()
  return c ? post(c, { m: 'saveImageFromBase64', a: [name, mime] }, base64) : false
}

/** Ask the app to open the system browser for a sign-in (`auth`) or a connect
 *  (`oauth`); another site than this one is confirmed by the person first.
 *  True only when the browser opened. */
export async function openNativeBrowser(url: string, kind: 'auth' | 'oauth'): Promise<boolean> {
  return (await askNative<boolean>('openAuthBrowser', [url, kind], NATIVE_BROWSER_TIMEOUT_MS)) === true
}

export function setNativeAuthInProgress(active: boolean) {
  callNative('setAuthInProgress', active)
}

let switchBusy = false

export function setNativeSwitchBusy(busy: boolean) {
  switchBusy = busy
  callNative('setSwitchBusy', busy)
}

/** The last switch-busy hint (a live meeting / voice call) — also what the
 * build-reload check defers on, native or not. */
export function isNativeSwitchBusy(): boolean {
  return switchBusy
}
