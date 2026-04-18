import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface TitleGenOption {
  provider: string
  model: string
  label: string
}

export interface TitleGeneration {
  enabled: boolean
  selected_model: string   // '' = Auto
  active: boolean          // enabled AND a Direct LLM provider resolves
  active_provider: string
  active_model: string
  options: TitleGenOption[]
}

// Whether automatic LLM chat-title generation is on, which model is pinned
// (''=Auto), whether it's currently active, and the configured-provider options.
// Source of truth is the Direct LLM execution layer — no separate API key.
export function useTitleGeneration() {
  return useQuery({
    queryKey: ['title-generation'],
    queryFn: async (): Promise<TitleGeneration> => {
      const res = await apiFetch('/v1/admin/title-generation')
      if (!res.ok) throw new Error('Failed to fetch title-generation settings')
      return res.json()
    },
  })
}

export function useSaveTitleGeneration() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: { enabled?: boolean; model?: string }) => {
      const res = await apiFetch('/v1/admin/title-generation', {
        method: 'PUT',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error('Failed to update title-generation settings')
      return res.json() as Promise<TitleGeneration>
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['title-generation'] }) },
  })
}
