import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../../api/auth'
import type { PlatformSettings, ConcurrencyStats, StorageUsage } from './PlatformPage.types'

export function usePlatformSettings() {
  return useQuery({
    queryKey: ['platform-settings'],
    queryFn: async (): Promise<PlatformSettings> => {
      const res = await apiFetch('/v1/admin/platform-settings')
      if (!res.ok) throw new Error('Failed to fetch')
      return res.json()
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
      if (!res.ok) throw new Error('Failed to save')
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['platform-settings'] })
    },
  })
}

// License is set via its own endpoint (persist + conditional activate), NOT the
// platform-settings PUT. Returns a message (e.g. activation_limit_reached).
export function useSetLicense() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (license_key: string) => {
      const res = await apiFetch('/v1/admin/license', {
        method: 'POST', body: JSON.stringify({ license_key }),
      })
      if (!res.ok) throw new Error('Failed to save license')
      return res.json() as Promise<{ message: string; status: string; activation_state: string }>
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['platform-settings'] }) },
  })
}

export function useLicenseAction(path: 'deactivate' | 'recheck') {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const res = await apiFetch(`/v1/admin/license/${path}`, { method: 'POST' })
      if (!res.ok) throw new Error('Failed')
      return res.json() as Promise<{ status: string; activation_state: string }>
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['platform-settings'] }) },
  })
}

export function useConcurrencyStats() {
  return useQuery({
    queryKey: ['concurrency-stats'],
    queryFn: async (): Promise<ConcurrencyStats> => {
      const res = await apiFetch('/v1/admin/concurrency-stats')
      if (!res.ok) throw new Error('Failed to fetch')
      return res.json()
    },
    refetchInterval: 5_000,
  })
}
export function useStorageUsage() {
  return useQuery({
    queryKey: ['storage-usage'],
    queryFn: async (): Promise<StorageUsage> => {
      const res = await apiFetch('/v1/admin/storage/usage')
      if (!res.ok) throw new Error('Failed to fetch')
      return res.json()
    },
  })
}
