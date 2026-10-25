import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../../api/auth'
import type { PlatformSettings, ConcurrencyStats, StorageUsage } from './PlatformPage.types'
import { apiJson, withMessage } from '../../lib/http/apiJson'

export function usePlatformSettings() {
  return useQuery({
    queryKey: ['platform-settings'],
    queryFn: async (): Promise<PlatformSettings> => {
      return apiJson<PlatformSettings>('/v1/admin/platform-settings').catch(withMessage('Failed to fetch'))
    },
  })
}

export function useSavePlatformSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: Record<string, unknown>) => {
      const res = await apiFetch('/v1/admin/platform-settings', {
        method: 'PUT',
        body: JSON.stringify(data),
      })
      if (!res.ok) {
        // A refused setting carries its reason (a 409 when turning the
        // second-factor requirement on would lock the admin out).
        let detail = ''
        try { detail = String((await res.json())?.detail || '') } catch { /* no body */ }
        throw new Error(detail || 'Failed to save')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['platform-settings'] }),
  })
}

// License is set via its own endpoint (persist + conditional activate), NOT the
// platform-settings PUT. Returns a message (e.g. activation_limit_reached).
export function useSetLicense() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (license_key: string) => {
      return apiJson<{ message: string; status: string; activation_state: string }>('/v1/admin/license', {
        method: 'POST', body: JSON.stringify({ license_key }),
      }).catch(withMessage('Failed to save license'))
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['platform-settings'] }) },
  })
}

export function useLicenseAction(path: 'deactivate' | 'recheck') {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      return apiJson<{ status: string; activation_state: string }>(`/v1/admin/license/${path}`, { method: 'POST' })
        .catch(withMessage('Failed'))
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['platform-settings'] }) },
  })
}

export function useConcurrencyStats() {
  return useQuery({
    queryKey: ['concurrency-stats'],
    queryFn: async (): Promise<ConcurrencyStats> => {
      return apiJson<ConcurrencyStats>('/v1/admin/concurrency-stats').catch(withMessage('Failed to fetch'))
    },
    refetchInterval: 5_000,
  })
}
export function useStorageUsage() {
  return useQuery({
    queryKey: ['storage-usage'],
    queryFn: async (): Promise<StorageUsage> => {
      return apiJson<StorageUsage>('/v1/admin/storage/usage').catch(withMessage('Failed to fetch'))
    },
  })
}
