/**
 * Row/list components for the Execution Layers tab split.
 *
 * Imports from .forms (AddModelForm). .forms does not import from here, so
 * there is no cycle.
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
import type { EngineProvider } from '../../api/engineDescriptor'
import { providerLabel } from '../../lib/engines'
import { Badge, SaveState } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'
import Switch from '../../components/ui/Switch'
import InfoTip from '../../components/ui/InfoTip'
import { AddModelForm } from './ExecutionLayersTab.forms'
import { SubscriptionWindowBars } from '../../components/engines/SubscriptionWindows'
import { TierMark } from '../../components/common/TierMark'
import { TIER_LABELS, tierTitle } from '../../lib/tiers'
import { ENGINE_SUBSCRIPTION_STATUS, type EngineSubscriptionStatus } from '../../lib/status/engineSubscription'
import { useDialogs } from '../../components/ui/dialogs'
import { toastError } from '../../lib/http/apiJson'
import Button from '../../components/ui/Button'
import { CARD_BORDERED, FIELD, FIELD_BORDER, FIELD_COMPACT } from '../../components/ui/classes'

const AUTH_TYPE_LABELS: Record<string, string> = {
  api_key: 'API Key',
  oauth: 'OAuth',
  local_endpoint: 'Local',
  relay: 'Hosted',
}

const STATUS_VARIANT: Record<EngineSubscriptionStatus, 'success' | 'warning' | 'danger'> = {
  active: 'success',
  disabled: 'warning',
  expired: 'danger',
}

// ---------------------------------------------------------------------------
// Subscription Row
// ---------------------------------------------------------------------------

export function SubscriptionRow({
  sub,
  layer,
  vendorId,
  onDiscover,
}: {
  sub: Subscription
  layer: string
  /** The engine's own vendor (`identity.vendor_id`; "" for a multi-provider
   *  engine). A row on the card's own vendor needs no provider badge; every
   *  other provider is named. */
  vendorId: string
  onDiscover?: (sub: Subscription) => void
}) {
  const dialogs = useDialogs()
  const updateMut = useUpdateSubscription()
  const { stateOf, run } = useSaveState()
  const deleteMut = useDeleteSubscription()

  // Editable only by the admin who connected it (is_mine) or for owner-less
  // platform infra (owner_sub=''); other admins' accounts are read-only.
  const ownerless = !sub.owner_sub
  const manageable = sub.is_mine || ownerless

  return (
    <div className="flex flex-wrap items-center gap-2 sm:gap-3 py-2 px-3 rounded-card hover:bg-surface-hover group">
      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-sm font-medium text-primary truncate">
            {sub.label || sub.oauth_email || `${sub.provider} ${AUTH_TYPE_LABELS[sub.auth_type] || sub.auth_type}`}
          </span>
          <Badge variant={STATUS_VARIANT[sub.status] || 'neutral'}>{sub.status}</Badge>
          <Badge>{AUTH_TYPE_LABELS[sub.auth_type] || sub.auth_type}</Badge>
          {sub.provider !== vendorId && <Badge variant="info">{sub.provider}</Badge>}
        </div>
        {sub.oauth_email && sub.label && (
          <p className="text-xs text-tertiary truncate">{sub.oauth_email}</p>
        )}
        {sub.auth_type === 'oauth' && <SubscriptionWindowBars windows={sub.windows} />}
        {/* Scope: who may use this account. An OAuth login can be used personally
            by its owner and/or contributed to the shared agent pool; only the owner
            (or any admin, for owner-less infra) can change this. */}
        {manageable ? (
          <div className="flex items-center gap-3 mt-1">
            {!ownerless && (
              <span className="inline-flex items-center">
                <label className="flex items-center gap-1 text-xs text-tertiary cursor-pointer">
                  <input
                    type="checkbox"
                    checked={sub.use_personal}
                    onChange={(e) => {
                      const on = e.target.checked
                      void run('personal', () => updateMut.mutateAsync({ layer, id: sub.id, use_personal: on }), { failTitle: 'The account setting was not saved' })
                    }}
                  />
                  Personal use
                </label>
                <InfoTip label="Personal use">The owner can use this account for their own chats</InfoTip>
                <SaveState state={stateOf('personal')} />
              </span>
            )}
            <span className="inline-flex items-center">
              <label className="flex items-center gap-1 text-xs text-tertiary cursor-pointer">
                <input
                  type="checkbox"
                  checked={sub.contribute_platform}
                  onChange={(e) => {
                    const on = e.target.checked
                    void run('shared', () => updateMut.mutateAsync({ layer, id: sub.id, contribute_platform: on }), { failTitle: 'The account setting was not saved' })
                  }}
                />
                Shared with agents
              </label>
              <InfoTip label="Shared with agents">Add this account to the shared subscriptions your agents use</InfoTip>
              <SaveState state={stateOf('shared')} />
            </span>
          </div>
        ) : (
          <p className="text-xs text-tertiary mt-1">Shared by another admin</p>
        )}
      </div>

      {/* Actions row */}
      <div className="flex items-center gap-2 shrink-0">
        {onDiscover && sub.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE && sub.auth_type !== 'oauth' && (
          <button
            onClick={() => onDiscover(sub)}
            className="text-xs text-link hover:text-link-hover transition-colors sm:opacity-0 sm:group-hover:opacity-100"
            title="Discover available models from this provider"
          >
            Discover
          </button>
        )}
        {manageable && (
          <button
            onClick={async () => {
              // The counter shown here can read stale; the server judges
              // "in use" on its live bindings and answers 409 only for
              // sessions that are really running — then the admin may
              // still remove it and let those sessions move.
              if (!(await dialogs.confirm({ title: 'Remove this subscription?', confirmLabel: 'Remove', tone: 'danger' }))) return
              try {
                await deleteMut.mutateAsync({ layer, id: sub.id })
              } catch (e) {
                const err = e as Error & { status?: number }
                if (err.status !== 409) {
                  toastError('The subscription was not removed')(e)
                  return
                }
                if (await dialogs.confirm({
                  title: 'Remove it anyway?',
                  body: `${err.message}\n\nIts running sessions move to another subscription, or stop until one is connected.`,
                  confirmLabel: 'Remove anyway',
                  tone: 'danger',
                })) {
                  deleteMut.mutate({ layer, id: sub.id, force: true })
                }
              }
            }}
            className="text-xs text-status-danger-fg hover:underline sm:opacity-0 sm:group-hover:opacity-100"
          >
            Remove
          </button>
        )}
      </div>
      {deleteMut.isError && (
        <p className="w-full text-xs text-status-danger-fg">{(deleteMut.error as Error).message}</p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Model Row
// ---------------------------------------------------------------------------

export function ModelRow({ model, layer, pricingEditable: enginePricesModels, xhighEditable }: {
  model: LayerModel
  layer: string
  /** The engine's `model_policy.pricing_editable`: its BYO keys are metered
   *  per model, so a custom row's price and context are the admin's to set
   *  (a CLI engine reports its own cost and has nothing to edit). */
  pricingEditable: boolean
  /** The row's provider declares xhigh per model (`effort_per_model`), so
   *  the flag is read at the wire and the checkbox means something. */
  xhighEditable: boolean
}) {
  const dialogs = useDialogs()
  const updateMut = useUpdateModel()
  const { stateOf, run } = useSaveState()
  const deleteMut = useDeleteModel()
  const [expanded, setExpanded] = useState(false)
  const [ctxWin, setCtxWin] = useState(String(model.context_window || ''))
  const [pIn, setPIn] = useState(String(model.pricing_input || ''))
  const [pOut, setPOut] = useState(String(model.pricing_output || ''))
  const [pCW, setPCW] = useState(String(model.pricing_cache_write || ''))
  const [pCR, setPCR] = useState(String(model.pricing_cache_read || ''))
  const [reasoning, setReasoning] = useState(!!model.supports_reasoning)
  const [xhigh, setXhigh] = useState(!!model.supports_xhigh)
  const [tier, setTier] = useState(model.tier ? String(model.tier) : '')
  const [goodAt, setGoodAt] = useState(model.good_at || '')

  const hasPricing = model.pricing_input > 0 || model.pricing_output > 0
  // Pricing and context are meaningful only on an engine that prices its
  // models (the descriptor says which); the tier and the "good at" line apply
  // to every custom row. Builtin rows are registry-authoritative for all of it.
  const pricingEditable = enginePricesModels && !model.is_builtin
  const tierEditable = !model.is_builtin

  const handleSave = () => {
    updateMut.mutateAsync({
      layer, id: model.id,
      ...(pricingEditable ? {
        context_window: ctxWin ? parseInt(ctxWin) : 0,
        pricing_input: pIn ? parseFloat(pIn) : 0,
        pricing_output: pOut ? parseFloat(pOut) : 0,
        pricing_cache_write: pCW ? parseFloat(pCW) : 0,
        pricing_cache_read: pCR ? parseFloat(pCR) : 0,
        supports_reasoning: reasoning,
        supports_xhigh: xhigh,
      } : {}),
      ...(tierEditable ? { tier: tier ? parseInt(tier) : null, good_at: goodAt.trim() } : {}),
    }).then(() => setExpanded(false)).catch(toastError('The model was not saved'))
  }

  const inputClass = `${FIELD} ${FIELD_COMPACT} ${FIELD_BORDER}`

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3 py-1.5 px-3 hover:bg-surface-hover group">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm text-primary font-mono truncate">{model.model_id}</span>
            {model.display_name !== model.model_id && (
              <span className="text-xs text-tertiary hidden sm:inline">{model.display_name}</span>
            )}
            {!model.is_builtin && <Badge variant="info">custom</Badge>}
            <span
              className="inline-flex items-center gap-1.5 text-3xs text-tertiary"
              title={tierTitle(model.tier, undefined, model.good_at)}
            >
              <TierMark tier={model.tier} goodAt={model.good_at} size="xs" />
              {model.tier ? `${TIER_LABELS[model.tier] || `tier ${model.tier}`}${model.good_at ? ` · ${model.good_at}` : ''}` : 'untiered'}
            </span>
            {hasPricing && enginePricesModels && (
              <span className="text-xs text-tertiary">
                ${model.pricing_input}/{model.pricing_output}
              </span>
            )}
          </div>
        </div>

        {/* Edit (custom models only — CLI reports its own cost, and builtin
            pricing/context/tier is registry-authoritative: the backend
            overwrites it from the registry on every sync, so editing it here
            would be silently reverted). */}
        {(pricingEditable || tierEditable) && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-xs text-secondary hover:text-link transition-colors opacity-0 group-hover:opacity-100"
            title={pricingEditable ? 'Edit pricing, context and tier' : 'Edit tier'}
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
            </svg>
          </button>
        )}

        {/* Enable/disable */}
        <SaveState state={stateOf('enabled')} />
        <Switch
          aria-label={model.model_id}
          checked={!!model.enabled}
          onChange={(v) => { void run('enabled', () => updateMut.mutateAsync({ layer, id: model.id, enabled: v }), { failTitle: v ? 'The model was not turned on' : 'The model was not turned off' }) }}
        />

        {/* Delete (custom only) */}
        {!model.is_builtin && (
          <button
            onClick={async () => {
              if (await dialogs.confirm({ title: `Remove the custom model "${model.model_id}"?`, confirmLabel: 'Remove', tone: 'danger' })) {
                deleteMut.mutateAsync({ layer, id: model.id }).catch(toastError('The model was not removed'))
              }
            }}
            className="text-xs text-status-danger-fg hover:underline opacity-0 group-hover:opacity-100"
          >
            Remove
          </button>
        )}
      </div>

      {/* Expanded editor: tier + "good at" for every custom row, pricing and
          context on the engines that price their models */}
      {expanded && (
        <div className="px-3 pb-2 pt-1 space-y-2">
          {tierEditable && (
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
              <div>
                <label className="text-3xs text-tertiary block mb-0.5">Capability tier</label>
                <select value={tier} onChange={(e) => setTier(e.target.value)} className={inputClass} aria-label="Capability tier">
                  <option value="">untiered</option>
                  <option value="1">1 · frontier</option>
                  <option value="2">2 · strong</option>
                  <option value="3">3 · balanced</option>
                  <option value="4">4 · fast</option>
                </select>
              </div>
              <div className="col-span-2 sm:col-span-4">
                <label className="text-3xs text-tertiary block mb-0.5">Good at: one line, shown to agents and in the pickers</label>
                <input type="text" maxLength={120} placeholder="Quick local answers with no cloud cost" value={goodAt} onChange={(e) => setGoodAt(e.target.value)} className={inputClass} aria-label="Good at" />
              </div>
            </div>
          )}
          {pricingEditable && (<>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
            <div className="col-span-2 sm:col-span-1">
              <label className="text-3xs text-tertiary block mb-0.5">Context Window</label>
              <input type="number" placeholder="e.g. 128000" value={ctxWin} onChange={(e) => setCtxWin(e.target.value)} className={inputClass} />
            </div>
            <div>
              <label className="text-3xs text-tertiary block mb-0.5">Input $/1M</label>
              <input type="number" step="0.01" placeholder="0" value={pIn} onChange={(e) => setPIn(e.target.value)} className={inputClass} />
            </div>
            <div>
              <label className="text-3xs text-tertiary block mb-0.5">Output $/1M</label>
              <input type="number" step="0.01" placeholder="0" value={pOut} onChange={(e) => setPOut(e.target.value)} className={inputClass} />
            </div>
            <div>
              <label className="text-3xs text-tertiary block mb-0.5">Cache W $/1M</label>
              <input type="number" step="0.01" placeholder="0" value={pCW} onChange={(e) => setPCW(e.target.value)} className={inputClass} />
            </div>
            <div>
              <label className="text-3xs text-tertiary block mb-0.5">Cache R $/1M</label>
              <input type="number" step="0.01" placeholder="0" value={pCR} onChange={(e) => setPCR(e.target.value)} className={inputClass} />
            </div>
          </div>
          <label className="flex items-center gap-2 text-xs text-secondary cursor-pointer">
            <input
              type="checkbox"
              checked={reasoning}
              onChange={(e) => setReasoning(e.target.checked)}
              className="accent-action"
            />
            Supports reasoning
          </label>
          {/* Only where the provider reads the flag — see AddModelForm. */}
          {xhighEditable && (
            <div className="text-xs text-secondary">
              <label className="cursor-pointer">
                <input
                  type="checkbox"
                  checked={xhigh}
                  onChange={(e) => setXhigh(e.target.checked)}
                  className="accent-action mr-2 align-middle"
                />
                Supports xhigh effort on Opus 4.7 and later
              </label>
              <InfoTip label="Supports xhigh effort on Opus 4.7 and later">Falls back to max on unsupported models.</InfoTip>
            </div>
          )}
          </>)}
          <div className="flex gap-2">
            <Button variant="primary" size="xs" onClick={handleSave} disabled={updateMut.isPending}>
              {updateMut.isPending ? 'Saving...' : 'Save'}
            </Button>
            <Button variant="ghost" size="xs" onClick={() => setExpanded(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

export function ModelsByProvider({
  models,
  layer,
  pricingEditable,
  providers,
  showAddModel,
  onAddCustom,
  onAddDone,
}: {
  models: LayerModel[]
  layer: string
  /** The engine's `model_policy.pricing_editable` (see ModelRow). */
  pricingEditable: boolean
  /** The engine's declared providers: the entry whose `effort_per_model`
   *  names xhigh is the one whose rows get the "Supports xhigh" checkbox
   *  (its adapter reads the flag; the others send xhigh to every model). */
  providers: EngineProvider[] | null
  showAddModel: string | false
  onAddCustom: (provider: string) => void
  onAddDone: () => void
}) {
  const xhighEditable = (provider: string) =>
    (providers?.find((p) => p.id === provider)?.effort_per_model ?? []).includes('xhigh')
  // Group models by provider
  const groups: Record<string, LayerModel[]> = {}
  for (const m of models) {
    const p = m.provider || 'anthropic'
    if (!groups[p]) groups[p] = []
    groups[p].push(m)
  }

  // The engine's declaration order, then any provider only a row names.
  const providerOrder = providers?.map((p) => p.id) ?? []
  const sorted = [
    ...providerOrder.filter((p) => groups[p]),
    ...Object.keys(groups).filter((p) => !providerOrder.includes(p)),
  ]

  if (sorted.length === 0) {
    return <p className="text-sm text-tertiary py-2">No models configured.</p>
  }

  return (
    <div className="space-y-3">
      {sorted.map((provider) => (
        <div key={provider}>
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs font-medium text-secondary">
              {providerLabel(providers, provider)}
            </span>
            <button
              onClick={() => onAddCustom(provider)}
              className="text-xs text-link hover:text-link-hover transition-colors"
            >
              + Custom
            </button>
          </div>
          {/* The add form opens under its provider's + Custom, above the
              models: a provider can list dozens. */}
          {showAddModel === provider && (
            <div className="mb-2">
              <AddModelForm layer={layer} provider={provider} xhighEditable={xhighEditable(provider)} onDone={onAddDone} />
            </div>
          )}
          <div className={`${CARD_BORDERED} overflow-hidden divide-y divide-subtle`}>
            {groups[provider].map((model) => (
              <ModelRow key={model.id} model={model} layer={layer} pricingEditable={pricingEditable} xhighEditable={xhighEditable(provider)} />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
