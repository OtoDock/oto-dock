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

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { type ChatAudioCapability } from '../audio/types'
import { probeNativeTtsAvailable } from '../audio/backends/nativeTts'
import { probeNativeSttAvailable, nativeSttProbeSettling } from '../audio/backends/nativeStt'
import { appleWebKitBrowser } from '../audio/device'
import { apiJson, withMessage } from '../lib/http/apiJson'

/** The capability request: the device's recognizers, and whether the
 * device recognizer is Apple's WebKit (the server then lets the platform be
 * the default where the policy allows both). */
export function capabilityQuery(hasNativeTts: boolean, hasNativeStt: boolean, appleWebKit: boolean): string {
  return `/v1/audio/capability?has_native_tts=${hasNativeTts}&has_native_stt=${hasNativeStt}`
    + (hasNativeStt && appleWebKit ? '&apple_webkit=true' : '')
}

export function useChatAudioCapability(enabled = true) {
  // `enabled=false` for pre-auth mounts (useWakeWord under RequireAuth) —
  // see useMyAudioPrefs: an unauthenticated 401 triggers apiFetch's
  // expired-session redirect and reload-loops the login page.
  const queryClient = useQueryClient()
  return useQuery({
    queryKey: ['audio-capability'],
    enabled,
    queryFn: async (): Promise<ChatAudioCapability> => {
      const [hasNativeTts, hasNativeStt] = await Promise.all([
        probeNativeTtsAvailable().catch(() => false),
        probeNativeSttAvailable().catch(() => false),
      ])
      // A recognizer that answered after the probe's wait is reported late:
      // the capability resolves again, so the microphone appears.
      if (!hasNativeStt) {
        void nativeSttProbeSettling()?.then((late) => {
          if (late) void queryClient.invalidateQueries({ queryKey: ['audio-capability'] })
        })
      }
      // Throw (not null) so a failed resolve retries and reads as an ERROR —
      // it must never render as "turned off by the administrator".
      return apiJson<ChatAudioCapability>(capabilityQuery(hasNativeTts, hasNativeStt, appleWebKitBrowser()))
        .catch(withMessage(() => 'Could not check the voice features on this device'))
    },
    staleTime: 30_000,
  })
}
