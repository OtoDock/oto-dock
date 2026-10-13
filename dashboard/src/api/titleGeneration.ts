import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiJson } from '../lib/http/apiJson'

export interface TitleGenOption {
  provider: string
  model: string
  label: string
}

/** The last title (or Check) since the proxy started: a configured provider
 * can still resolve no credential or answer an error. */
export interface TitleGenAttempt {
  ok: boolean
  /** `title`: a title made or refused by the provider; `credential`: only
   * whether the credential resolves (a title's lookup, or Check, which
   * calls no model). Absent on a proxy from before the field. */
  kind?: 'title' | 'credential'
  provider: string
  model: string
  reason: string
  at: string
}

export interface TitleGeneration {
  enabled: boolean
  selected_model: string   // '' = Auto
  active: boolean          // enabled AND a Direct LLM provider is configured
  active_provider: string
  active_model: string
  options: TitleGenOption[]
  /** Absent on an older proxy, null before the first attempt. */
  last_attempt?: TitleGenAttempt | null
  /** The pinned model when no model row lists it any more (the registry
   * dropped it): titles run on Auto. '' otherwise; absent on an older proxy. */
  unlisted_pin?: string
}

// Whether automatic LLM chat-title generation is on, which model is pinned
// (''=Auto), whether it's currently active, and the configured-provider options.
// Source of truth is the Direct LLM execution layer — no separate API key.
export function useTitleGeneration() {
  return useQuery({
    queryKey: ['title-generation'],
    queryFn: () => apiJson<TitleGeneration>('/v1/admin/title-generation', {},
      'Failed to fetch title-generation settings'),
  })
}

export function useSaveTitleGeneration() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: { enabled?: boolean; model?: string }) =>
      apiJson<TitleGeneration>('/v1/admin/title-generation', { method: 'PUT', json: data },
        'Failed to update title-generation settings'),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['title-generation'] }) },
  })
}

/** Resolve the title provider's credential now (no model is called); the
 * answer is the status with the outcome as `last_attempt`. */
export function useCheckTitleGeneration() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => apiJson<TitleGeneration>('/v1/admin/title-generation/check', { method: 'POST' },
      'Could not check title generation'),
    onSuccess: (status) => { qc.setQueryData(['title-generation'], status) },
  })
}
