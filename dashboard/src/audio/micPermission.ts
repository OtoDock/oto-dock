// Microphone permission for chat dictation on the Android app.
//
// Background: the native speech-recognition plugin talks to Android's
// SpeechRecognizer directly; its own requestPermissions() doesn't reliably
// surface the OS dialog inside the Capacitor shell. So to OBTAIN the grant we
// trigger Capacitor's own flow — a getUserMedia({audio}) call routes through
// BridgeWebChromeClient.onPermissionRequest, which requests RECORD_AUDIO
// (+ MODIFY_AUDIO_SETTINGS) via the OS dialog, exactly like image-upload/location.
//
// Once the permission is granted we must NOT open getUserMedia again: the WebView
// (Chromium) keeps the mic stream warm for a moment after we stop the tracks, and
// the native recognizer starts right after — so it would record into a mic still
// held by Chromium. Hence: prime ONLY when not yet granted; once granted, skip.
//
// Pure web — ships on an app reload, no APK rebuild.

import { isNativePlatform } from './types'

export async function ensureNativeMicPermission(): Promise<void> {
  // Desktop/browser: Web Speech and the platform path prompt on their own.
  if (!isNativePlatform()) return

  // Already granted → do nothing (avoid the mic-contention described above).
  try {
    const status = await navigator.permissions?.query?.({ name: 'microphone' as PermissionName })
    if (status?.state === 'granted') return
  } catch { /* Permissions API unavailable here → fall through to the primer */ }

  if (!navigator.mediaDevices?.getUserMedia) return // very old WebView → let the plugin try
  try {
    // echoCancellation:false — a grant-only stream must not flip Android
    // into MODE_IN_COMMUNICATION (in-call volume slider) even transiently;
    // only hardware-AEC-attached captures trigger that mode.
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false },
    })
    // We only needed the grant — release the mic immediately.
    stream.getTracks().forEach(t => t.stop())
  } catch (e) {
    const name = (e as { name?: string })?.name
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      throw new Error('Microphone permission denied. Enable it in Settings → Apps → OtoDock → Permissions.')
    }
    throw new Error('Could not access the microphone on this device.')
  }
}
