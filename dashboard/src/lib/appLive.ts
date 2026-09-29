// In-process pub/sub for the live-app WS frames (MINI-APPS.md "Live apps").
//
// useDashboardWs emits here when the server relays an agent's push, a new
// state document, or a request to open an app on this screen. The frames
// are consumed far from the socket (an AppFrame deep in the overlay, the
// overlay controller, the app shell's toast), so, like fileUpdates.ts, the
// bus is decoupled from React state: emit is a plain fan-out and each
// subscriber manages its own lifecycle.

import { WIRE } from '../api/wireEvents'
import type { AppDeployedFrame, AppPushFrame, AppStateFrame, CatalogFrame, OpenAppFrame } from '../api/wireEvents'

// The five frames are the wire's (api/wireEvents.ts: `app_push` carries the
// relay time as information only; `app_deployed` is a new release being
// served; `catalog` is one feed change — a delta row or a whole snapshot,
// `seq` per (user, agent, feed), a gap re-requests the snapshot). Re-exported
// for the consumers that read them off this bus.
export type { AppDeployedFrame, AppPushFrame, AppStateFrame, CatalogFrame, OpenAppFrame }

/** The socket reopened: every live server feed re-requests its snapshot.
 *  Dashboard-synthesised — never on the wire, so not in `WIRE`. */
export const CATALOG_RESYNC = 'catalog_resync'
export interface CatalogResyncFrame {
  type: typeof CATALOG_RESYNC
}

export type AppLiveFrame = AppPushFrame | AppStateFrame | OpenAppFrame | AppDeployedFrame | CatalogFrame | CatalogResyncFrame

type Listener<T> = (frame: T) => void

const pushListeners = new Set<Listener<AppPushFrame>>()
const stateListeners = new Set<Listener<AppStateFrame>>()
const openListeners = new Set<Listener<OpenAppFrame>>()
const deployedListeners = new Set<Listener<AppDeployedFrame>>()
const catalogListeners = new Set<Listener<CatalogFrame | CatalogResyncFrame>>()

function fanOut<T>(set: Set<Listener<T>>, frame: T): void {
  set.forEach((l) => {
    try {
      l(frame)
    } catch {
      /* one subscriber throwing must not break the fan-out */
    }
  })
}

export function emitAppLive(frame: AppLiveFrame): void {
  if (frame.type === WIRE.APP_PUSH) fanOut(pushListeners, frame)
  else if (frame.type === WIRE.APP_STATE) fanOut(stateListeners, frame)
  else if (frame.type === WIRE.OPEN_APP) fanOut(openListeners, frame)
  else if (frame.type === WIRE.APP_DEPLOYED) fanOut(deployedListeners, frame)
  else if (frame.type === WIRE.CATALOG || frame.type === CATALOG_RESYNC) fanOut(catalogListeners, frame)
}

export function onAppDeployed(listener: Listener<AppDeployedFrame>): () => void {
  deployedListeners.add(listener)
  return () => { deployedListeners.delete(listener) }
}

export function onCatalog(listener: Listener<CatalogFrame | CatalogResyncFrame>): () => void {
  catalogListeners.add(listener)
  return () => { catalogListeners.delete(listener) }
}

export function onAppPush(listener: Listener<AppPushFrame>): () => void {
  pushListeners.add(listener)
  return () => { pushListeners.delete(listener) }
}

export function onAppState(listener: Listener<AppStateFrame>): () => void {
  stateListeners.add(listener)
  return () => { stateListeners.delete(listener) }
}

export function onOpenApp(listener: Listener<OpenAppFrame>): () => void {
  openListeners.add(listener)
  return () => { openListeners.delete(listener) }
}

// ── Catalog subscriptions ────────────────────────────────────────────────────
// A catalog delta reaches this tab only for the (agent, feed) pairs it asked
// for: the server keeps the set per connection, so it is empty after a
// reconnect and the socket re-sends it on open. Counted per pair, so two
// frames on the same feed hold one subscription.

type FrameSender = (frame: Record<string, unknown>) => void
let catalogSender: FrameSender | null = null
const catalogSubs = new Map<string, { agent: string; feed: string; count: number }>()

function sendCatalog(type: 'catalog_subscribe' | 'catalog_unsubscribe', agent: string, feed: string): void {
  if (!catalogSender) return
  try { catalogSender({ type, agent, feed }) } catch { /* socket gone; the reconnect re-sends */ }
}

export function setCatalogSender(s: FrameSender | null): void {
  catalogSender = s
}

/** A closing socket lets go of its own sender only (see releaseFocusSender). */
export function releaseCatalogSender(s: FrameSender): void {
  if (catalogSender === s) catalogSender = null
}

export function subscribeCatalog(agent: string, feed: string): void {
  const key = `${agent}\u0000${feed}`
  const cur = catalogSubs.get(key)
  if (cur) { cur.count += 1; return }
  catalogSubs.set(key, { agent, feed, count: 1 })
  sendCatalog('catalog_subscribe', agent, feed)
}

export function unsubscribeCatalog(agent: string, feed: string): void {
  const key = `${agent}\u0000${feed}`
  const cur = catalogSubs.get(key)
  if (!cur) return
  cur.count -= 1
  if (cur.count > 0) return
  catalogSubs.delete(key)
  sendCatalog('catalog_unsubscribe', agent, feed)
}

/** After a (re)connect the server holds no subscriptions: send them again. */
export function resendCatalogSubscriptions(): void {
  for (const s of catalogSubs.values()) sendCatalog('catalog_subscribe', s.agent, s.feed)
}

export function _resetCatalogSubsForTests(): void {
  catalogSubs.clear()
  catalogSender = null
}
