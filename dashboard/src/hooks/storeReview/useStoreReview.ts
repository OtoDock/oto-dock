/**
 * The store review ask's two hooks: the app root's mount and the chat page's
 * turn watch. The rules and the record live in lib/device/storeReview.ts.
 */

import { useEffect } from 'react'
import { Capacitor } from '@capacitor/core'
import { hasNativeBridge } from '../../lib/nativeBridge'
import {
  noteStoreReviewError, noteTurnEnded, noteTurnStreaming, setStoreReviewSettings, startStoreReview,
} from '../../lib/device/storeReview'
import { WIRE, type WireFrame, type WireType } from '../../api/wireEvents'
import { useFlag } from '../useFlag'

/** The chat page's socket registry (`useDashboardWs().subscribe`). */
export type FrameSubscribe = (frameType: WireType, fn: (msg: WireFrame) => void) => () => void

/**
 * The chat page's turns, for the reply moment: a turn that streamed text in
 * this page, and whether it ended with no error and no stop. A `done` also
 * comes on a resume or a re-attach; one with no text before it counts
 * nothing. A refused queued message is no failed turn. An error frame or a
 * failed warm-up starts the quiet window.
 */
export function useStoreReviewTurns(subscribe: FrameSubscribe): void {
  useEffect(() => {
    let streamed: string | null = null
    let clean = true
    const offs = [
      subscribe(WIRE.TEXT, (msg) => {
        if (msg.type !== WIRE.TEXT || streamed === (msg.chat_id ?? '')) return
        streamed = msg.chat_id ?? ''
        clean = true
        noteTurnStreaming(streamed)
      }),
      subscribe(WIRE.ERROR, (msg) => {
        if (msg.type !== WIRE.ERROR || msg.queue_id) return
        clean = false
        noteStoreReviewError()
      }),
      subscribe(WIRE.ABORTED, () => { clean = false }),
      subscribe(WIRE.WARMUP_FAILED, () => { noteStoreReviewError() }),
      subscribe(WIRE.DONE, (msg) => {
        if (msg.type === WIRE.DONE && streamed !== null && streamed === (msg.chat_id ?? '')) {
          noteTurnEnded(streamed, clean)
        }
        streamed = null
        clean = true
      }),
    ]
    return () => offs.forEach((off) => off())
  }, [subscribe])
}

/**
 * Mounted once at the authenticated app root. `sub` is the person, null
 * until they are signed in and past the forced password and second-factor
 * screens. Nothing runs in a browser, and nothing while the admin's switch
 * is off: the record is neither read nor written then.
 */
export function useStoreReview(sub: string | null): void {
  const on = useFlag('store_review_enabled')
  const daysBetween = useFlag('store_review_days')
  useEffect(() => { setStoreReviewSettings(on, daysBetween) }, [on, daysBetween])
  useEffect(() => {
    if (!sub || !on || !Capacitor.isNativePlatform() || !hasNativeBridge()) return
    return startStoreReview(sub)
  }, [sub, on])
}
