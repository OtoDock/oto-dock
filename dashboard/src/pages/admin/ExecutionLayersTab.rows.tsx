/**
 * Row/list components for the Execution Layers tab split.
 *
 * Imports from the leaf widgets module (Toggle, Badge, PROVIDER_LABELS) and from
 * .forms (AddModelForm). .forms does not import from here, so there is no cycle.
 */

import { useState } from 'react'
import {
  useUpdateSubscription,
  useDeleteSubscription,
  useUpdateModel,
  useDeleteModel,
  type Subscription,
  type LayerModel,
} from '../../api/executionLayers'
import { Toggle, Badge, PROVIDER_LABELS } from './ExecutionLayersTab.widgets'
import { AddModelForm } from './ExecutionLayersTab.forms'

const AUTH_TYPE_LABELS: Record<string, string> = {
  api_key: 'API Key',
  oauth: 'OAuth',
  local_endpoint: 'Local',
  relay: 'Hosted',
}

const STATUS_VARIANT: Record<string, 'green' | 'amber' | 'red'> = {
  active: 'green',
  disabled: 'amber',
  expired: 'red',
}

// ---------------------------------------------------------------------------
// Subscription Row
// ---------------------------------------------------------------------------

export function SubscriptionRow({
  sub,
  layer,
  onDiscover,
}: {
  sub: Subscription
  layer: string
  onDiscover?: (sub: Subscription) => void
}) {
  const updateMut = useUpdateSubscription()
  const deleteMut = useDeleteSubscription()

  // Editable only by the admin who connected it (is_mine) or for owner-less
  // platform infra (owner_sub=''); other admins' accounts are read-only.
  const ownerless = !sub.owner_sub
  const manageable = sub.is_mine || ownerless

  return (
    <div className="flex flex-wrap items-center gap-2 sm:gap-3 py-2 px-3 rounded-lg hover:bg-p-bg-hover/50 group">
      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-sm font-medium text-p-text truncate">
            {sub.label || sub.oauth_email || `${sub.provider} ${AUTH_TYPE_LABELS[sub.auth_type] || sub.auth_type}`}
          </span>
          <Badge variant={STATUS_VARIANT[sub.status] || 'default'}>{sub.status}</Badge>
          <Badge>{AUTH_TYPE_LABELS[sub.auth_type] || sub.auth_type}</Badge>
          {sub.provider !== 'anthropic' && <Badge variant="blue">{sub.provider}</Badge>}
          {sub.is_primary ? <Badge variant="blue">primary</Badge> : null}
        </div>
        {sub.oauth_email && sub.label && (
          <p className="text-xs text-p-text-light truncate">{sub.oauth_email}</p>
        )}
        {/* Scope: who may use this account. An OAuth login can be used personally
            by its owner and/or contributed to the shared agent pool; only the owner
            (or any admin, for owner-less infra) can change this. */}
        {manageable ? (
          <div className="flex items-center gap-3 mt-1">
            {!ownerless && (
              <label className="flex items-center gap-1 text-xs text-p-text-light cursor-pointer" title="The owner can use this account for their own chats">
                <input
                  type="checkbox"
                  checked={sub.use_personal}
                  onChange={(e) => updateMut.mutate({ layer, id: sub.id, use_personal: e.target.checked })}
                />
                Personal use
              </label>
            )}
            <label className="flex items-center gap-1 text-xs text-p-text-light cursor-pointer" title="Contribute this account to the shared agent pool">
              <input
                type="checkbox"
                checked={sub.contribute_platform}
                onChange={(e) => updateMut.mutate({ layer, id: sub.id, contribute_platform: e.target.checked })}
              />
              Agent pool
            </label>
          </div>
        ) : (
          <p className="text-xs text-p-text-light mt-1">Shared by another admin</p>
        )}
      </div>

      {/* Actions row */}
      <div className="flex items-center gap-2 shrink-0">
        {onDiscover && sub.status === 'active' && sub.auth_type !== 'oauth' && (
          <button
            onClick={() => onDiscover(sub)}
            className="text-xs text-brand hover:text-brand-hover transition-colors sm:opacity-0 sm:group-hover:opacity-100"
            title="Discover available models from this provider"
          >
            Discover
          </button>
        )}
        {manageable && !sub.is_primary && (
          <button
            onClick={() => updateMut.mutate({ layer, id: sub.id, is_primary: true })}
            className="text-xs text-p-text-secondary hover:text-brand transition-colors sm:opacity-0 sm:group-hover:opacity-100"
            title="Set as primary"
          >
            Primary
          </button>
        )}
        {manageable && (
          <button
            onClick={() => {
              if (sub.active_sessions > 0) {
                alert(`Cannot delete: ${sub.active_sessions} active sessions`)
                return
              }
              if (confirm('Remove this subscription?')) {
                deleteMut.mutate({ layer, id: sub.id })
              }
            }}
            className="text-xs text-red-500 hover:text-red-600 transition-colors sm:opacity-0 sm:group-hover:opacity-100"
          >
            Remove
          </button>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Model Row
// ---------------------------------------------------------------------------

export function ModelRow({ model, layer }: { model: LayerModel; layer: string }) {
  const updateMut = useUpdateModel()
  const deleteMut = useDeleteModel()
  const [expanded, setExpanded] = useState(false)
  const [ctxWin, setCtxWin] = useState(String(model.context_window || ''))
  const [pIn, setPIn] = useState(String(model.pricing_input || ''))
  const [pOut, setPOut] = useState(String(model.pricing_output || ''))
  const [pCW, setPCW] = useState(String(model.pricing_cache_write || ''))
  const [pCR, setPCR] = useState(String(model.pricing_cache_read || ''))
  const [reasoning, setReasoning] = useState(!!model.supports_reasoning)
  const [xhigh, setXhigh] = useState(!!model.supports_xhigh)

  const hasPricing = model.pricing_input > 0 || model.pricing_output > 0

  const handleSavePricing = () => {
    updateMut.mutate({
      layer, id: model.id,
      context_window: ctxWin ? parseInt(ctxWin) : 0,
      pricing_input: pIn ? parseFloat(pIn) : 0,
      pricing_output: pOut ? parseFloat(pOut) : 0,
      pricing_cache_write: pCW ? parseFloat(pCW) : 0,
      pricing_cache_read: pCR ? parseFloat(pCR) : 0,
      supports_reasoning: reasoning,
      supports_xhigh: xhigh,
    }, { onSuccess: () => setExpanded(false) })
  }

  const inputClass = "w-full px-2 py-1 text-xs border border-p-border-light rounded-sm bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-1 focus:ring-brand/30"

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3 py-1.5 px-3 hover:bg-p-bg-hover/50 group">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm text-p-text font-mono truncate">{model.model_id}</span>
            {model.display_name !== model.model_id && (
              <span className="text-xs text-p-text-light hidden sm:inline">{model.display_name}</span>
            )}
            {!model.is_builtin && <Badge variant="blue">custom</Badge>}
            {hasPricing && layer === 'direct-llm' && (
              <span className="text-xs text-p-text-light">
                ${model.pricing_input}/{model.pricing_output}
              </span>
            )}
          </div>
        </div>

        {/* Pricing edit (direct-llm custom models only — CLI reports its own
            cost, and builtin pricing/context is registry-authoritative: the
            backend overwrites it from MODEL_REGISTRY on every sync, so editing
            it here would be silently reverted). */}
        {layer === 'direct-llm' && !model.is_builtin && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-xs text-p-text-secondary hover:text-brand transition-colors opacity-0 group-hover:opacity-100"
            title="Edit pricing & context"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
            </svg>
          </button>
        )}

        {/* Enable/disable */}
        <Toggle
          checked={!!model.enabled}
          onChange={(v) => updateMut.mutate({ layer, id: model.id, enabled: v })}
        />

        {/* Delete (custom only) */}
        {!model.is_builtin && (
          <button
            onClick={() => {
              if (confirm(`Remove custom model "${model.model_id}"?`)) {
                deleteMut.mutate({ layer, id: model.id })
              }
            }}
            className="text-xs text-red-500 hover:text-red-600 transition-colors opacity-0 group-hover:opacity-100"
          >
            Remove
          </button>
        )}
      </div>

      {/* Expanded pricing editor */}
      {expanded && (
        <div className="px-3 pb-2 pt-1 space-y-2">
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
            <div className="col-span-2 sm:col-span-1">
              <label className="text-[10px] text-p-text-light block mb-0.5">Context Window</label>
              <input type="number" placeholder="e.g. 128000" value={ctxWin} onChange={(e) => setCtxWin(e.target.value)} className={inputClass} />
            </div>
            <div>
              <label className="text-[10px] text-p-text-light block mb-0.5">Input $/1M</label>
              <input type="number" step="0.01" placeholder="0" value={pIn} onChange={(e) => setPIn(e.target.value)} className={inputClass} />
            </div>
            <div>
              <label className="text-[10px] text-p-text-light block mb-0.5">Output $/1M</label>
              <input type="number" step="0.01" placeholder="0" value={pOut} onChange={(e) => setPOut(e.target.value)} className={inputClass} />
            </div>
            <div>
              <label className="text-[10px] text-p-text-light block mb-0.5">Cache W $/1M</label>
              <input type="number" step="0.01" placeholder="0" value={pCW} onChange={(e) => setPCW(e.target.value)} className={inputClass} />
            </div>
            <div>
              <label className="text-[10px] text-p-text-light block mb-0.5">Cache R $/1M</label>
              <input type="number" step="0.01" placeholder="0" value={pCR} onChange={(e) => setPCR(e.target.value)} className={inputClass} />
            </div>
          </div>
          <label className="flex items-center gap-2 text-xs text-p-text-secondary cursor-pointer">
            <input
              type="checkbox"
              checked={reasoning}
              onChange={(e) => setReasoning(e.target.checked)}
              className="rounded-sm border-p-border-light"
            />
            Supports reasoning
          </label>
          {/* Only meaningful for Anthropic — see AddModelForm comment. */}
          {model.provider === 'anthropic' && (
            <label className="flex items-center gap-2 text-xs text-p-text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={xhigh}
                onChange={(e) => setXhigh(e.target.checked)}
                className="rounded-sm border-p-border-light"
              />
              Supports xhigh effort (Opus 4.7+). Falls back to max on unsupported models.
            </label>
          )}
          <div className="flex gap-2">
            <button onClick={handleSavePricing} disabled={updateMut.isPending} className="px-2 py-1 text-xs rounded-sm bg-brand text-white hover:bg-brand-hover transition-colors disabled:opacity-40">
              {updateMut.isPending ? 'Saving...' : 'Save'}
            </button>
            <button onClick={() => setExpanded(false)} className="px-2 py-1 text-xs rounded-sm text-p-text-secondary hover:bg-p-bg-hover transition-colors">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export function ModelsByProvider({
  models,
  layer,
  showAddModel,
  onAddCustom,
  onAddDone,
}: {
  models: LayerModel[]
  layer: string
  showAddModel: string | false
  onAddCustom: (provider: string) => void
  onAddDone: () => void
}) {
  // Group models by provider
  const groups: Record<string, LayerModel[]> = {}
  for (const m of models) {
    const p = m.provider || 'anthropic'
    if (!groups[p]) groups[p] = []
    groups[p].push(m)
  }

  const providerOrder = ['anthropic', 'openai', 'groq', 'ollama', 'openai_compatible']
  const sorted = [
    ...providerOrder.filter((p) => groups[p]),
    ...Object.keys(groups).filter((p) => !providerOrder.includes(p)),
  ]

  if (sorted.length === 0) {
    return <p className="text-sm text-p-text-light py-2">No models configured.</p>
  }

  return (
    <div className="space-y-3">
      {sorted.map((provider) => (
        <div key={provider}>
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs font-medium text-p-text-secondary">
              {PROVIDER_LABELS[provider] || provider}
            </span>
            <button
              onClick={() => onAddCustom(provider)}
              className="text-xs text-brand hover:text-brand-hover transition-colors"
            >
              + Custom
            </button>
          </div>
          <div className="rounded-lg border border-p-border-light bg-white dark:bg-p-surface overflow-hidden divide-y divide-p-border-light">
            {groups[provider].map((model) => (
              <ModelRow key={model.id} model={model} layer={layer} />
            ))}
          </div>
          {/* Add model form renders directly below the provider it belongs to */}
          {showAddModel === provider && (
            <AddModelForm layer={layer} provider={provider} onDone={onAddDone} />
          )}
        </div>
      ))}
    </div>
  )
}
