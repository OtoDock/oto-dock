/**
 * Admin Execution Layers tab — manages subscriptions and models per layer.
 *
 * One expandable card per execution layer. Each card shows:
 * - Subscriptions (API keys, OAuth, local endpoints) with add/remove
 * - Models (builtin + custom) with enable/disable toggles
 */

import { useState } from 'react'
import {
  useAdminExecutionLayers,
  useDiscoverModels,
  type ExecutionLayerInfo,
  type Subscription,
  type DiscoveredModel,
} from '../../api/executionLayers'
import { Badge } from './ExecutionLayersTab.widgets'
import { AddApiKeyForm, AddLocalEndpointForm, ConnectOAuth, DiscoverModelsPanel } from './ExecutionLayersTab.forms'
import { SubscriptionRow, ModelsByProvider } from './ExecutionLayersTab.rows'
import { SetupBanner } from './ExecutionLayersTab.sections'

// ---------------------------------------------------------------------------
// Layer Card
// ---------------------------------------------------------------------------

function LayerCard({ layer }: { layer: ExecutionLayerInfo }) {
  const [expanded, setExpanded] = useState(false)
  const [showAddApiKey, setShowAddApiKey] = useState(false)
  const [showAddEndpoint, setShowAddEndpoint] = useState<string | null>(null) // provider name
  const [showAddModel, setShowAddModel] = useState<string | false>(false)
  const [showOAuth, setShowOAuth] = useState(false)
  const [discoverState, setDiscoverState] = useState<{
    sub: Subscription
    models: DiscoveredModel[] | null
    provider: string
  } | null>(null)

  const discoverMut = useDiscoverModels()

  const isDirectLlm = layer.name === 'direct-llm'
  // Vendor chip shown before the engine name (mirrors User Settings → AI Engines).
  // Only the single-vendor coding CLIs get one; direct-llm is multi-provider.
  const vendorBadge = layer.name === 'claude-code-cli' ? 'Anthropic'
    : layer.name === 'codex-cli' ? 'OpenAI'
    : null
  const subs = layer.subscriptions.platform
  const activeSubs = subs.filter((s) => s.status === 'active').length
  const oauthCount = subs.filter((s) => s.auth_type === 'oauth').length
  const apiKeyCount = subs.filter((s) => s.auth_type === 'api_key').length
  const endpointCount = subs.filter((s) => s.auth_type === 'local_endpoint').length
  const hostedCount = subs.filter((s) => s.auth_type === 'relay' && s.status === 'active').length
  // Hosted (relay) subs render in their own toggle box; the row list shows only
  // bring-your-own credentials (keys / local endpoints / OAuth).
  const rowSubs = isDirectLlm ? subs.filter((s) => s.auth_type !== 'relay') : subs

  // Determine available providers for local endpoints (Codex layer)
  const providers = (layer.capabilities as Record<string, unknown>).providers as
    | { id: string; label: string; requires_key?: boolean }[]
    | null
  const localProviders = providers?.filter((p) => !p.requires_key) ?? []

  // Determine the main provider for API keys
  const mainProvider = layer.name === 'codex-cli' ? 'openai'
    : layer.name === 'claude-code-cli' ? 'anthropic'
    : 'anthropic'

  // Existing model IDs for the discover panel
  const existingModelIds = new Set(layer.models.map((m) => m.model_id))

  const handleDiscover = (sub: Subscription) => {
    setDiscoverState({ sub, models: null, provider: sub.provider })
    discoverMut.mutate(
      { layer: layer.name, subscriptionId: sub.id },
      {
        onSuccess: (data) => {
          setDiscoverState({ sub, models: data.models, provider: data.provider })
        },
        onError: () => {
          // Keep panel open so error is visible
        },
      },
    )
  }

  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-4 sm:px-5 py-3 sm:py-4 text-left hover:bg-p-bg-hover/30 transition-colors"
      >
        <div className="flex flex-wrap items-center gap-2 sm:gap-3 min-w-0">
          {vendorBadge && (
            <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-p-bg text-p-text-secondary border border-p-border-light">
              {vendorBadge}
            </span>
          )}
          <h3 className="text-sm font-semibold text-p-text">
            {vendorBadge ? layer.display_name.replace(/^(OpenAI|Anthropic)\s+/i, '') : layer.display_name}
          </h3>
          <Badge variant={activeSubs > 0 ? 'green' : 'default'}>
            {[
              hostedCount > 0 && `${hostedCount} hosted`,
              oauthCount > 0 && `${oauthCount} sub${oauthCount !== 1 ? 's' : ''}`,
              apiKeyCount > 0 && `${apiKeyCount} key${apiKeyCount !== 1 ? 's' : ''}`,
              endpointCount > 0 && `${endpointCount} endpoint${endpointCount !== 1 ? 's' : ''}`,
            ].filter(Boolean).join(', ') || 'No connections'}
          </Badge>
          {layer.subscriptions.user_count > 0 && (
            <span className="text-xs text-p-text-light hidden sm:inline">{layer.subscriptions.user_count} user sub{layer.subscriptions.user_count !== 1 ? 's' : ''}</span>
          )}
        </div>
        <svg
          className={`w-4 h-4 text-p-text-secondary transition-transform shrink-0 ${expanded ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Body */}
      {expanded && (
        <div className="px-4 sm:px-5 pb-4 sm:pb-5 space-y-5 border-t border-p-border-light pt-4">

          {/* Subscriptions Section — bring-your-own credentials */}
          <div>
            <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
              <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider">{isDirectLlm ? 'Bring your own key' : 'Subscriptions'}</h4>
              <div className="flex flex-wrap gap-2">
                {(layer.name === 'claude-code-cli' || layer.name === 'codex-cli') && (
                  <button
                    onClick={() => { setShowOAuth(!showOAuth); setShowAddApiKey(false); setShowAddEndpoint(null) }}
                    className="text-xs text-brand hover:text-brand-hover transition-colors"
                  >
                    + Connect Account
                  </button>
                )}
                <button
                  onClick={() => { setShowAddApiKey(!showAddApiKey); setShowAddEndpoint(null); setShowOAuth(false) }}
                  className="text-xs text-brand hover:text-brand-hover transition-colors"
                >
                  + API Key
                </button>
                {localProviders.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => { setShowAddEndpoint(showAddEndpoint === p.id ? null : p.id); setShowAddApiKey(false); setShowOAuth(false) }}
                    className="text-xs text-brand hover:text-brand-hover transition-colors"
                  >
                    + {p.label}
                  </button>
                ))}
              </div>
            </div>

            {rowSubs.length === 0 && !showAddApiKey && !showAddEndpoint && !showOAuth && (
              <p className="text-sm text-p-text-light py-2">
                {isDirectLlm
                  ? 'No API keys configured.'
                  : 'No platform subscriptions configured.'}
              </p>
            )}

            <div className="space-y-0.5">
              {rowSubs.map((sub) => (
                <SubscriptionRow
                  key={sub.id}
                  sub={sub}
                  layer={layer.name}
                  onDiscover={handleDiscover}
                />
              ))}
            </div>

            {showOAuth && (
              <ConnectOAuth layer={layer.name} ownerType="platform" provider={layer.name === 'codex-cli' ? 'openai' : 'claude'} onDone={() => setShowOAuth(false)} />
            )}
            {showAddApiKey && (
              <AddApiKeyForm layer={layer.name} provider={mainProvider} onDone={() => setShowAddApiKey(false)} />
            )}
            {showAddEndpoint && (
              <AddLocalEndpointForm layer={layer.name} provider={showAddEndpoint} onDone={() => setShowAddEndpoint(null)} />
            )}

            {/* Discover models: loading state */}
            {discoverState && !discoverState.models && (
              <div className="mt-3 p-4 bg-p-bg rounded-xl border border-p-border-light">
                {discoverMut.isPending ? (
                  <div className="flex items-center gap-3">
                    <svg className="animate-spin h-4 w-4 text-brand" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                    </svg>
                    <span className="text-sm text-p-text-secondary">
                      Fetching models from {discoverState.provider}...
                    </span>
                  </div>
                ) : discoverMut.isError ? (
                  <div className="space-y-2">
                    <p className="text-sm text-red-500">{(discoverMut.error as Error).message}</p>
                    <button
                      onClick={() => setDiscoverState(null)}
                      className="text-xs text-p-text-secondary hover:text-p-text transition-colors"
                    >
                      Dismiss
                    </button>
                  </div>
                ) : null}
              </div>
            )}

            {/* Discover models: results panel */}
            {discoverState?.models && (
              <DiscoverModelsPanel
                layer={layer.name}
                provider={discoverState.provider}
                discoveredModels={discoverState.models}
                existingModelIds={existingModelIds}
                onDone={() => setDiscoverState(null)}
              />
            )}
          </div>

          {/* Models Section */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider">Models</h4>
            </div>

            <ModelsByProvider
              models={layer.models}
              layer={layer.name}
              showAddModel={showAddModel as string | false}
              onAddCustom={(provider) => setShowAddModel(showAddModel === provider ? false : provider)}
              onAddDone={() => setShowAddModel(false)}
            />
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main tab component
// ---------------------------------------------------------------------------

export default function ExecutionLayersTab() {
  const { data: layers, isLoading, error } = useAdminExecutionLayers()

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading execution layers...</p>
  if (error) return <p className="text-sm text-red-500">Failed to load execution layers.</p>
  if (!layers || layers.length === 0) return <p className="text-sm text-p-text-secondary">No execution layers found.</p>

  // Direct-LLM renders last (it's the supporting layer — title gen / phone
  // classifier — not a primary coding agent). Array.sort is stable, so the
  // other layers keep their backend order.
  const orderedLayers = [...layers].sort(
    (a, b) => (a.name === 'direct-llm' ? 1 : 0) - (b.name === 'direct-llm' ? 1 : 0),
  )

  // The setup banner clears once a coding agent has an active platform sub.
  const codingReady = layers.some(
    (l) =>
      (l.name === 'claude-code-cli' || l.name === 'codex-cli') &&
      l.subscriptions.platform.some((s) => s.status === 'active'),
  )

  return (
    <div className="space-y-4">
      {!codingReady && <SetupBanner />}
      <p className="text-sm text-p-text-light">
        Configure the platform's Anthropic and OpenAI subscriptions (used for
        agent-scoped tasks and chats) plus API keys and models for each AI engine.
        Users connect their own subscriptions for their own chats in User Settings;
        platform API keys are available to users you've granted platform auth.
      </p>
      {orderedLayers.map((layer) => (
        <LayerCard key={layer.name} layer={layer} />
      ))}
    </div>
  )
}
