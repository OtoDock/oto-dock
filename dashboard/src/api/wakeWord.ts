// Wake-word keyword feed — the proxy pre-encodes "hey <display name>" into
// the BPE token lines the on-device spotter requires (raw text is rejected
// by the engine). The @slug tag inside each line rides back verbatim in a
// detection, so the payload's keywords string is both the model input and
// the routing table.

import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface WakeAgent {
  slug: string
  display_name: string
  // False when the display name has no English-phonetic residue (the model
  // is English-BPE) or collides with an earlier agent's phrase.
  wakeable: boolean
}

export interface WakeKeywords {
  enabled: boolean
  reason: string
  threshold: number
  keywords: string // newline-joined "<tokens> [:boost] [#threshold] @<slug>" lines
  agents: WakeAgent[]
  platform_target: string | null // what "hey OtoDock" resolves to
}

export function useWakeKeywords(enabled: boolean) {
  return useQuery({
    queryKey: ['wake-keywords'],
    queryFn: async (): Promise<WakeKeywords> => {
      const res = await apiFetch('/v1/users/me/wake-keywords')
      if (!res.ok) throw new Error('Failed to fetch wake keywords')
      return res.json()
    },
    enabled,
    // Renames/new agents re-encode automatically; the listener rebuilds the
    // spotter when the keywords string actually changes.
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}
