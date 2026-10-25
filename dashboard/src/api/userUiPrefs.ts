// Per-user roaming UI preferences — a small server-side JSON bag so sticky
// dashboard choices follow the user across devices. First tenant:
// `last_execution_mode` (agent slug → 'interactive' | '-p'), the roamed copy
// of agentPrefsStore.lastInteractive. Mirrors api/userAudio.ts; localStorage
// remains the offline fallback (the store hydrates from this once per session
// load and writes through on toggle).

import { useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { hydrateLastInteractiveFromServer } from '../store/agentPrefsStore'
import { setFocusSharing } from '../lib/focus'
import { apiJson, withMessage } from '../lib/http/apiJson'

export interface UiPrefs {
  // agent slug → 'interactive' | '-p'
  last_execution_mode?: Record<string, string>
  // Owner notifications at 90% and 100% of a connected account's weekly
  // window (absent = on).
  subscription_usage_alerts?: boolean
  // Live apps (absent = on): the agent's turn names the app on screen, and
  // an agent may open one of its apps on this user's screen.
  share_focus_with_agents?: boolean
  agents_may_open_apps?: boolean
  [key: string]: unknown
}

export function useMyUiPrefs() {
  return useQuery({
    queryKey: ['my-ui-prefs'],
    queryFn: async (): Promise<UiPrefs> => {
      return apiJson<UiPrefs>('/v1/users/me/ui-prefs').catch(withMessage('Failed to fetch UI preferences'))
    },
  })
}

// PUT is a server-side SHALLOW merge: provided top-level keys replace the
// stored ones wholesale, everything else is kept.
export function useUpdateMyUiPrefs() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: UiPrefs) => {
      // The server's sentence when it gave one, so the toast says why.
      return apiJson('/v1/users/me/ui-prefs', {
        method: 'PUT',
        body: JSON.stringify(data),
      }, 'Your settings were not saved. Try again.')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-ui-prefs'] }),
  })
}

// Hydrate the sticky agent-prefs store from the server bag. Mounted from
// AgentChat (the surface that consumes the sticky map); the ONCE-per-session
// guard lives in the store module, so repeated mounts/refetches are no-ops.
export function useHydrateUiPrefs() {
  const { data } = useMyUiPrefs()
  useEffect(() => {
    if (!data) return
    hydrateLastInteractiveFromServer(data.last_execution_mode)
    // The interactive rail stamps the focus line itself, so it needs the
    // user's setting here; the server applies the same setting to its turns.
    setFocusSharing(data.share_focus_with_agents !== false)
  }, [data])
}
