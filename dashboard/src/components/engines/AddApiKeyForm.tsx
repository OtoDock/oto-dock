import { useState } from 'react'
import type { EngineProvider } from '../../api/engineDescriptor'

// The API-key form both AI Engines cards use: the admin tab binds it to the
// admin add mutation, the user card to the user add mutation (the key kept
// out of the agent pool). The providers a key may belong to come from the
// engine's descriptor (lib/engines keyProviders): one provider → no select,
// several → a select with the first preselected. The server refuses a
// provider the engine does not list, so the form offers only those.

export interface ApiKeyVars {
  layer: string
  provider: string
  auth_type: string
  label: string
  api_key: string
  contribute_platform?: boolean
}

export interface ApiKeyMutation {
  mutate: (vars: ApiKeyVars, opts?: { onSuccess?: () => void }) => void
  isPending: boolean
  isError: boolean
  error: unknown
}

export function ApiKeyForm({ layer, providers, onDone, mutation, ownerType }: {
  layer: string
  providers: EngineProvider[]
  onDone: () => void
  mutation: ApiKeyMutation
  ownerType: 'platform' | 'user'
}) {
  const [label, setLabel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [provider, setProvider] = useState(providers[0]?.id ?? '')
  const showProviderSelect = providers.length > 1

  const handleSubmit = () => {
    if (!apiKey.trim()) return
    const vars: ApiKeyVars = { layer, provider, auth_type: 'api_key', label: label.trim(), api_key: apiKey.trim() }
    if (ownerType === 'user') vars.contribute_platform = false
    mutation.mutate(vars, { onSuccess: () => { setLabel(''); setApiKey(''); onDone() } })
  }

  return (
    <div className="mt-3 p-3 bg-p-bg rounded-lg border border-p-border-light space-y-2">
      {showProviderSelect && (
        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          className="w-full px-3 py-1.5 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
        >
          {providers.map((p) => (
            <option key={p.id} value={p.id}>{p.label}</option>
          ))}
        </select>
      )}
      <input
        type="text"
        placeholder="Label (optional)"
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        className="w-full px-3 py-1.5 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
      />
      <input
        type="password"
        placeholder="API key"
        aria-label="API key"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        className="w-full px-3 py-1.5 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 font-mono"
      />
      <div className="flex gap-2">
        <button
          onClick={handleSubmit}
          disabled={!apiKey.trim() || mutation.isPending}
          className="px-3 py-1.5 text-sm rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors disabled:opacity-40"
        >
          {mutation.isPending ? 'Adding...' : 'Add'}
        </button>
        <button
          onClick={onDone}
          className="px-3 py-1.5 text-sm rounded-lg text-p-text-secondary hover:bg-p-bg-hover transition-colors"
        >
          Cancel
        </button>
      </div>
      {mutation.isError && (
        <p className="text-xs text-red-500">{(mutation.error as Error).message}</p>
      )}
    </div>
  )
}
