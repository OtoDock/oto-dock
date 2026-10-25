// The install's OtoDock connection as the platform settings report it:
// the connection card, the low-credit prompt and the engines tab read it.

import { useQuery } from '@tanstack/react-query'
import { apiJson, withMessage } from '../../lib/http/apiJson'

export interface OtodockConnection {
  connected: boolean
  enabled: boolean
  active: boolean
  relay_available: boolean
  air_gapped: boolean
  forced: boolean
}

/** Shares the ['platform-settings'] query cache with PlatformPage's hook. */
export function useOtodockConnection(enabled = true) {
  return useQuery({
    queryKey: ['platform-settings'],
    queryFn: async (): Promise<{ otodock_connection?: OtodockConnection }> => {
      return apiJson<{ otodock_connection?: OtodockConnection }>('/v1/admin/platform-settings')
        .catch(withMessage('Failed to fetch'))
    },
    enabled,
  })
}
