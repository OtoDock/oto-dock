// The audio session a spoken answer plays in on an iPhone or an iPad.
// Apple's WebKit gives a page that plays through Web Audio alone the
// "ambient" session, and the Ring/Silent switch mutes it. A spoken answer
// the person asked for plays as a video does: every PCM player holds the
// "playback" type (navigator.audioSession) while it plays, and the last one
// to stop puts "auto" back, so the session can end and other apps' sound
// can go on.
//
// Two WebKit rules shape the rest. A "playback" type makes every microphone
// request fail, and it replaces the session of a capture that is already
// live, which cuts that microphone. So the type is never asked for while a
// microphone is open or opening (a live capture already plays through the
// switch, in "play-and-record"), and it is put back the moment one opens.
// A Mac's Safari, Android and every browser without the API never ask.

import { isAppleTouch } from '../lib/device/viewport'
import { captureLive, subscribeMic } from './micCoordinator'

interface AudioSessionLike { type: string }
type SessionHints = Parameters<typeof isAppleTouch>[0] & { audioSession?: AudioSessionLike }

let holds = 0
let asked: AudioSessionLike | null = null
let unsubscribe: (() => void) | null = null

function putBack(): void {
  const session = asked
  if (!session) return
  asked = null
  unsubscribe?.()
  unsubscribe = null
  try {
    // Only our own value goes: a type someone else set since then stays.
    if (session.type === 'playback') session.type = 'auto'
  } catch { /* the page plays on as it is */ }
}

/** Hold the playback session for one player: call it as the player starts
 * and the returned function as it stops (idempotent). */
export function holdPlaybackSession(nav: SessionHints = navigator): () => void {
  const session = nav.audioSession
  if (!session || !isAppleTouch(nav)) return () => {}
  holds++
  if (!asked && !captureLive() && session.type === 'auto') {
    try {
      session.type = 'playback'
      asked = session
      unsubscribe = subscribeMic(() => { if (captureLive()) putBack() })
    } catch { /* the speech plays as it did before */ }
  }
  let released = false
  return () => {
    if (released) return
    released = true
    holds--
    if (holds === 0) putBack()
  }
}

/** Test hook: forget every hold without touching a session. */
export function _resetPlaybackSession(): void {
  holds = 0
  asked = null
  unsubscribe?.()
  unsubscribe = null
}
