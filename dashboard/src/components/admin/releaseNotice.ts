// The daily release check as the dashboard sees it (GET /v1/admin/release-notice):
// the running version, the latest release found and whether it is newer. Read
// by the admin banner and the OtoDock tab's "This install" card; "Check now"
// posts /v1/admin/release-check and replaces the cached answer.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiJson, withMessage } from '../../lib/http/apiJson'

export interface ReleaseNotice {
  enabled: boolean
  // Why the check is off ('' when it runs): air-gapped, the cloud, the key.
  reason: string
  running: string
  latest: string
  url: string
  published_at: string
  checked_at: string
  error: string
  newer: boolean
}

export const RELEASE_NOTICE_KEY = ['release-notice']
// The public upgrading guide: the banner's "How to upgrade" and the notification point at it.
export const UPGRADE_GUIDE_URL = 'https://docs.otodock.io/administration/upgrading'
// localStorage: the version the admin dismissed the banner for; a newer one shows again.
export const RELEASE_BANNER_DISMISSED = 'otodock.releaseBanner.dismissed'

export function useReleaseNotice(enabled: boolean) {
  return useQuery({
    queryKey: RELEASE_NOTICE_KEY,
    queryFn: async (): Promise<ReleaseNotice> => {
      return apiJson<ReleaseNotice>('/v1/admin/release-notice').catch(withMessage('Failed to fetch'))
    },
    enabled,
    staleTime: 30 * 60_000,
    refetchInterval: 60 * 60_000,
  })
}

export function useReleaseCheck() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (): Promise<ReleaseNotice> => {
      return apiJson<ReleaseNotice>('/v1/admin/release-check', { method: 'POST' }).catch(withMessage('Failed'))
    },
    onSuccess: (data) => { qc.setQueryData(RELEASE_NOTICE_KEY, data) },
  })
}
