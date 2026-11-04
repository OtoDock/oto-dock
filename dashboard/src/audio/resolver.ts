// Pick the concrete backend from the server-resolved capability + the user's
// device mode. Mirrors the server's availability semantics.

import { type ChatAudioCapability, type AudioMode, type TTSBackend, type STTBackend, AUTOMATIC_ON_PLATFORM } from './types'
import { nativeTts } from './backends/nativeTts'
import { platformTts } from './backends/platformTts'
import { nativeStt } from './backends/nativeStt'
import { platformStt } from './backends/platformStt'
import { appleWebKitBrowser } from './device'
import { isAutoLanguage } from './lang'

export function resolveTtsBackend(cap: ChatAudioCapability, mode: AudioMode): TTSBackend | null {
  if (cap.tts === 'unavailable') return null
  if (cap.tts === 'native_only') return nativeTts.isAvailable() ? nativeTts : null
  if (cap.tts === 'platform') return platformTts
  // 'either' — user choice
  if (mode === 'native') return nativeTts.isAvailable() ? nativeTts : platformTts
  if (mode === 'platform') return platformTts
  return nativeTts.isAvailable() ? nativeTts : platformTts  // auto → prefer native
}

// The dictation's engine for the capability, the person's mode and the
// stored language. The Automatic language runs on the platform whenever the
// capability says a provider is there for it (`stt_automatic`: a device
// recognizer cannot identify a language), unless the person picked the
// device, whose Automatic is then the device's own language. On Apple's
// WebKit the platform is the default pick where both are allowed.
export function resolveSttBackend(
  cap: ChatAudioCapability, mode: AudioMode, language?: string | null, appleWebKit: boolean = appleWebKitBrowser(),
): STTBackend | null {
  const platform = platformStt.isAvailable() ? platformStt : null
  const device = nativeStt.isAvailable() ? nativeStt : null
  if (cap.stt === 'unavailable') return null
  if (cap.stt === 'platform') return platform
  const automatic = isAutoLanguage(language) && cap.stt_automatic === AUTOMATIC_ON_PLATFORM && !!platform
  if (cap.stt === 'native_only') return automatic && mode !== 'native' ? platform : device
  // 'either' — user choice
  if (mode === 'native') return device ?? platform
  if (mode === 'platform') return platform
  if (automatic || appleWebKit) return platform ?? device
  return device ?? platform
}
