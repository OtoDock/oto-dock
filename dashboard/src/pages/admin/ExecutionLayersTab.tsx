/**
 * Admin Execution Layers tab — manages subscriptions and models per layer.
 *
 * One expandable card per execution layer. Each card shows:
 * - Subscriptions (API keys, OAuth) with add/remove
 * - Local models (self-hosted endpoints, listed once, enabled per engine) —
 *   the same section inside the Direct LLM API and Codex CLI cards
 * - Models (builtin + custom) with enable/disable toggles
 */

import { Fragment, useState } from 'react'
import {
  useAdminExecutionLayers,
  useDiscoverModels,
  type ExecutionLayerInfo,
  type Subscription,
  type DiscoveredModel,
} from '../../api/executionLayers'
import { Badge } from '../../components/ui/SettingsControls'
import { AddApiKeyForm, ConnectOAuth, DiscoverModelsPanel } from './ExecutionLayersTab.forms'
import { LocalModelsSection } from './ExecutionLayersTab.local'
import { SubscriptionRow, ModelsByProvider } from './ExecutionLayersTab.rows'
import { SetupBanner } from './ExecutionLayersTab.sections'
import { BalanceHint } from '../../components/engines/SubscriptionWindows'
import {
  acceptsLocalEndpoints, acceptsRelay, codingEngineNames, engineLabel, isCoding, isLocalProvider,
  keyProviders, sortEngineRows, supportsOAuth, vendorBadge,
} from '../../lib/engines'
import { ENGINE_SUBSCRIPTION_STATUS } from '../../lib/status/engineSubscription'
import { CARD } from '../../components/ui/classes'

// ---------------------------------------------------------------------------
// Layer Card
// ---------------------------------------------------------------------------

function LayerCard({ layer }: { layer: ExecutionLayerInfo }) {
  const [expanded, setExpanded] = useState(false)
  const [showAddApiKey, setShowAddApiKey] = useState(false)
  const [showAddModel, setShowAddModel] = useState<string | false>(false)
  const [showOAuth, setShowOAuth] = useState(false)
  const [discoverState, setDiscoverState] = useState<{
    sub: Subscription
    models: DiscoveredModel[] | null
    provider: string
    // Engines the discovered models are added to — a shared local endpoint
    // adds to every engine it is enabled for; undefined = this card's engine.
    layers?: string[]
  } | null>(null)

  const discoverMut = useDiscoverModels()

  // Everything the card decides about the engine comes from its descriptor
  // (lib/engines.ts), never from the id: the relay-capable engine gets the
  // hosted section and the "bring your own key" wording, an engine with a
  // login gets Connect Account, the vendor chip is its vendor (none for a
  // multi-provider engine, which mirrors User Settings → AI Engines).
  const engine = layer.capabilities
  const relayCapable = acceptsRelay(engine)
  const vendor = vendorBadge(engine)
  const label = engineLabel(engine)
  const subs = layer.subscriptions.platform
  const activeSubs = subs.filter((s) => s.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE).length
  const oauthCount = subs.filter((s) => s.auth_type === 'oauth').length
  const apiKeyCount = subs.filter((s) => s.auth_type === 'api_key').length
  const hostedCount = subs.filter((s) => s.auth_type === 'relay' && s.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE).length
  // Hosted (relay) subs render in their own toggle box; the row list shows only
  // bring-your-own credentials (keys / OAuth). Local endpoints live in the
  // shared Local models section (the server lists them once, not per layer).
  const rowSubs = relayCapable ? subs.filter((s) => s.auth_type !== 'relay') : subs
  const hasLocalModels = acceptsLocalEndpoints(engine)
  // The providers an admin key on this engine may belong to (its vendor,
  // or the key-taking providers of a multi-provider engine).
  const providers = keyProviders(engine)

  // Existing model IDs for the discover panel
  const existingModelIds = new Set(layer.models.map((m) => m.model_id))

  const handleDiscover = (sub: Subscription, layers?: string[]) => {
    setDiscoverState({ sub, models: null, provider: sub.provider, layers })
    discoverMut.mutate(
      { layer: layer.name, subscriptionId: sub.id },
      {
        onSuccess: (data) => {
          setDiscoverState({ sub, models: data.models, provider: data.provider, layers })
        },
        onError: () => {
          // Keep panel open so error is visible
        },
      },
    )
  }

  // The discover panel (loading / error / results) renders under the row it
  // was launched from: a key row, or a local server's row in the shared Local
  // models section. Under a whole list it would open out of sight, and above
  // the local section it would read as the key list's.
  const isLocalDiscover = discoverState != null && isLocalProvider(engine, discoverState.provider)
  const discoverPanel = discoverState ? (
    <>
      {/* Discover models: loading state */}
      {!discoverState.models && (
        <div className="mt-3 p-4 bg-surface-sunken rounded-card">
          {discoverMut.isPending ? (
            <div className="flex items-center gap-3">
              <svg className="animate-spin h-4 w-4 text-secondary" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
              </svg>
              <span className="text-sm text-secondary">
                Fetching models from {discoverState.provider}...
              </span>
            </div>
          ) : discoverMut.isError ? (
            <div className="space-y-2">
              <p className="text-sm text-status-danger-fg">{(discoverMut.error as Error).message}</p>
              <button
                onClick={() => setDiscoverState(null)}
                className="text-xs text-secondary hover:text-primary transition-colors"
              >
                Dismiss
              </button>
            </div>
          ) : null}
        </div>
      )}

      {/* Discover models: results panel */}
      {discoverState.models && (
        <DiscoverModelsPanel
          layer={layer.name}
          provider={discoverState.provider}
          discoveredModels={discoverState.models}
          existingModelIds={existingModelIds}
          onDone={() => setDiscoverState(null)}
          layers={discoverState.layers}
        />
      )}
    </>
  ) : null

  return (
    <div className={`${CARD} overflow-hidden`}>
      {/* Header */}
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-4 sm:px-5 py-3 sm:py-4 text-left hover:bg-surface-hover transition-colors"
      >
        <div className="flex flex-wrap items-center gap-2 sm:gap-3 min-w-0">
          {vendor && (
            <span className="shrink-0 text-3xs font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-surface-muted text-secondary">
              {vendor}
            </span>
          )}
          <h3 className="text-sm font-semibold text-primary">{label}</h3>
          <Badge variant={activeSubs > 0 ? 'success' : 'neutral'}>
            {[
              hostedCount > 0 && `${hostedCount} hosted`,
              oauthCount > 0 && `${oauthCount} subscription${oauthCount !== 1 ? 's' : ''}`,
              apiKeyCount > 0 && `${apiKeyCount} key${apiKeyCount !== 1 ? 's' : ''}`,
            ].filter(Boolean).join(', ') || 'No connections'}
          </Badge>
          {layer.subscriptions.user_count > 0 && (
            <span className="text-xs text-tertiary hidden sm:inline">{layer.subscriptions.user_count} user subscription{layer.subscriptions.user_count !== 1 ? 's' : ''}</span>
          )}
        </div>
        <svg
          className={`w-4 h-4 text-secondary transition-transform shrink-0 ${expanded ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Body */}
      {expanded && (
        <div className="px-4 sm:px-5 pb-4 sm:pb-5 space-y-5 border-t border-subtle pt-4">

          {/* Subscriptions Section — bring-your-own credentials */}
          <div>
            <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
              <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider">{relayCapable ? 'Bring your own key' : 'Subscriptions'}</h4>
              <div className="flex flex-wrap gap-2">
                {supportsOAuth(engine) && (
                  <button
                    onClick={() => { setShowOAuth(!showOAuth); setShowAddApiKey(false) }}
                    className="text-xs text-link hover:text-link-hover transition-colors"
                  >
                    + Connect Account
                  </button>
                )}
                <button
                  onClick={() => { setShowAddApiKey(!showAddApiKey); setShowOAuth(false) }}
                  className="text-xs text-link hover:text-link-hover transition-colors"
                >
                  + API Key
                </button>
              </div>
            </div>

            {/* The key form opens under its button, above the rows. */}
            {showAddApiKey && (
              <AddApiKeyForm layer={layer.name} providers={providers} onDone={() => setShowAddApiKey(false)} />
            )}

            {rowSubs.length === 0 && !showAddApiKey && !showOAuth && (
              <p className="text-sm text-tertiary py-2">
                {relayCapable
                  ? 'No API keys configured.'
                  : 'No platform subscriptions configured.'}
              </p>
            )}

            <div className="space-y-0.5">
              {rowSubs.map((sub) => (
                <Fragment key={sub.id}>
                  <SubscriptionRow
                    sub={sub}
                    layer={layer.name}
                    vendorId={engine.identity.vendor_id}
                    onDiscover={handleDiscover}
                  />
                  {!isLocalDiscover && discoverState?.sub.id === sub.id && discoverPanel}
                </Fragment>
              ))}
            </div>
            <BalanceHint oauthCount={rowSubs.filter((s) => s.auth_type === 'oauth' && s.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE).length} />

            {/* A connect is several steps: it opens in a dialog. */}
            {showOAuth && (
              <ConnectOAuth engine={engine} ownerType="platform" onDone={() => setShowOAuth(false)} />
            )}
          </div>

          {/* Local models — shared across the engines that dial OpenAI-compatible servers */}
          {hasLocalModels && (
            <div>
              <LocalModelsSection
                layer={layer.name}
                onDiscover={(t) => handleDiscover(
                  { id: t.id, provider: t.provider, layer: layer.name } as Subscription, t.layers,
                )}
                belowRow={(id) => (isLocalDiscover && discoverState?.sub.id === id ? discoverPanel : null)}
              />
            </div>
          )}

          {/* Models Section */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider">Models</h4>
            </div>

            <ModelsByProvider
              models={layer.models}
              layer={layer.name}
              pricingEditable={engine.model_policy.pricing_editable}
              providers={engine.providers}
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

  if (isLoading) return <p className="text-sm text-secondary">Loading AI engines...</p>
  if (error) return <p className="text-sm text-status-danger-fg">Could not load the AI engines. Try again in a minute.</p>
  if (!layers || layers.length === 0) return <p className="text-sm text-secondary">No AI engines found.</p>

  // The engines in their declared order (`identity.sort_order`: the coding
  // engines first, the supporting one — title gen / phone classifier — last).
  const orderedLayers = sortEngineRows(layers)

  // The setup banner clears once a coding engine has an active platform sub;
  // it names the coding engines from their descriptors.
  const codingReady = layers.some(
    (l) => isCoding(l.capabilities) && l.subscriptions.platform.some((s) => s.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE),
  )

  return (
    <div className="space-y-4">
      {!codingReady && <SetupBanner engines={codingEngineNames(layers.map((l) => l.capabilities))} />}
      <p className="text-sm text-tertiary">
        Set up the shared Anthropic and OpenAI subscriptions your agents use for
        tasks and chats. Add API keys and models for each AI engine. People connect
        their own subscriptions for their own chats in User Settings, AI Engines.
        In Admin, Users, the Platform API switch lets a person use these API keys.
      </p>
      {orderedLayers.map((layer) => (
        <LayerCard key={layer.name} layer={layer} />
      ))}
    </div>
  )
}
