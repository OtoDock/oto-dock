// Resolve what the chat sound/mic icons can do on THIS device: report native
// availability to the server, which applies the admin policy + feature flag and
// returns the capability. Read on-demand (short staleTime) so a mid-session
// policy/flag change is picked up.
//
// Native availability is PROBED, not assumed: inside the APK the plugins are
// asked whether the device can actually recognize/speak (a phone without a
// RecognitionService or TTS voices used to report native anyway, and the mic
// died silently — operator report 2026-07-12). The probes are memoized in the
// backends and run inside queryFn, so the key stays static and the first
// capability resolve simply waits the extra round-trip once.

import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '../api/auth'
import { type ChatAudioCapability } from '../audio/types'
import { probeNativeTtsAvailable } from '../audio/backends/nativeTts'
import { probeNativeSttAvailable } from '../audio/backends/nativeStt'

export function useChatAudioCapability() {
  return useQuery({
    queryKey: ['audio-capability'],
    queryFn: async (): Promise<ChatAudioCapability> => {
      const [hasNativeTts, hasNativeStt] = await Promise.all([
        probeNativeTtsAvailable().catch(() => false),
        probeNativeSttAvailable().catch(() => false),
      ])
      const res = await apiFetch(
        `/v1/audio/capability?has_native_tts=${hasNativeTts}&has_native_stt=${hasNativeStt}`,
      )
      // Throw (not null) so a failed resolve retries and reads as an ERROR —
      // it must never render as "turned off by the administrator".
      if (!res.ok) throw new Error(`audio capability failed: HTTP ${res.status}`)
      return res.json()
    },
    staleTime: 30_000,
  })
}
