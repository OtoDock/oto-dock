// Which browser family this page runs in, as far as dictation cares: Apple's
// WebKit exposes `webkitSpeechRecognition` like Chrome does, but its
// recognizer is Apple's (one language per start, its own ends), so the
// capability and the resolver treat it apart.

import { isNativePlatform } from './types'

// A Blink browser (Chrome, Edge, Opera, every Android browser and the
// Android WebView) names Chrome in its user agent; jsdom names itself.
const NOT_APPLE = /Chrome\/|Chromium\/|Edg\/|OPR\/|Android|jsdom/
// Every browser on iOS is WebKit, Chrome (CriOS) and Firefox (FxiOS) too.
const APPLE_MOBILE = /iPhone|iPad|iPod|CriOS|FxiOS/

/** Apple's WebKit: Safari on a Mac, an iPad or an iPhone, and every browser
 * on iOS. An iPad in desktop mode (the default) reports a Macintosh, so
 * macOS Safari counts as well. Desktop Chrome, Edge, Firefox, Android's
 * browsers and the Android WebView read false. */
export function isAppleWebKit(ua: string): boolean {
  if (APPLE_MOBILE.test(ua)) return true
  return /AppleWebKit/.test(ua) && !NOT_APPLE.test(ua)
}

/** This page's answer. False inside a Capacitor app: its recognizer is the
 * plugin, never WebKit's. */
export function appleWebKitBrowser(): boolean {
  if (typeof navigator === 'undefined' || isNativePlatform()) return false
  return isAppleWebKit(navigator.userAgent || '')
}
