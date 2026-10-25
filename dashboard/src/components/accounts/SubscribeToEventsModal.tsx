/**
 * "Subscribe to events" modal.
 *
 * Three logical steps in one modal:
 *   1. Vendor target — driven by manifest's `vendor_target_spec.kind`
 *      (free_text + regex / static_list dropdown). remote_list pickers
 *      are deferred until the list endpoint lands. A manifest with
 *      `target_kinds` (GitHub: one repository, or every repository in an
 *      organization) puts the choice above the field; the chosen kind's
 *      hints and regex replace the flat ones.
 *   2. Event picker — checkbox list from manifest's `event_catalog`. When
 *      the account's recorded scopes lack what the picked events need, one
 *      line above the list says which permission and what to tick.
 *   3. Confirm — POST /v1/subscriptions; errors surface inline.
 *
 * Scope-agnostic: the same modal creates a personal subscription or one for
 * an agent (`scope='service'`). From Connected Accounts a "Subscribe as"
 * control offers the agents this account serves as service identity; from
 * the agent's MCPs tab the agent is fixed.
 */

import { useState } from 'react'
import { safeHref } from '../../lib/safeUrl'
import type { ServiceBindingSummary } from '../../api/credentials'
import {
  SubscriptionRequestError,
  useCreateSubscription,
  type CreateSubscriptionRequest,
  type WebhookEventCatalogEntry,
  type VendorTargetSpec,
} from '../../api/subscriptions'
import Dialog from '../ui/Dialog'
import Button from '../ui/Button'
import { Input, Select } from '../ui/Field'
import { BADGE_BASE, BADGE_TONE, DIALOG_PANEL } from '../ui/classes'
import InfoTip from '../ui/InfoTip'


interface Props {
  mcpName: string
  accountLabel: string
  providerId: string
  eventCatalog: WebhookEventCatalogEntry[]
  vendorTargetSpec: VendorTargetSpec
  /** EFFECTIVE registration mode for this account (relay = hosted delivery,
   * zero vendor-console steps). */
  registrationMode?: 'relay' | 'auto' | 'manual' | 'app'
  /** The target is a GitHub App installation: the picker's help says how
   * its events arrive, so no delivery line shows. */
  installationTarget?: boolean
  manualInstructionsUrl?: string
  /** Prefill for the vendor-target input (slack: the account's team_id). */
  vendorTargetPrefill?: string
  /** 'service' subscribes for `agent` with the bound account, no choice
   * offered; 'user' (default) is personal unless `serviceOptions` adds
   * agents to choose from. */
  scope?: 'user' | 'service'
  agent?: string
  /** Agents this account serves as service identity. Non-empty renders the
   * "Subscribe as" control (personal stays the default). */
  serviceOptions?: ServiceBindingSummary[]
  /** The account's recorded scopes; null or absent when not known. */
  grantedScopes?: string[] | null
  /** Each scope a service asks for, with that service's label. */
  scopeServices?: Record<string, string>
  onClose: () => void
}

const PERSONAL = '__me__'

/** Plain words for a refused subscription request. Structured refusals
 * (missing scopes, a duplicate) say what to do, the scopes in the scope
 * line's words; anything else is the server's message. */
function describeSubscriptionError(
  err: unknown,
  opts: ScopeContext,
): string {
  if (err instanceof SubscriptionRequestError && typeof err.detail !== 'string') {
    const d = err.detail
    if (d.error === 'missing_scopes') {
      const missing = Array.isArray(d.required) ? d.required.map(String) : []
      return scopeNotice(missing, opts)
    }
    if (d.error === 'exists') {
      return 'A subscription for this target already exists here.'
    }
  }
  return (err as Error).message
}

function joinAnd(words: string[]): string {
  return words.length < 2
    ? words.join('')
    : `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`
}

interface ScopeContext {
  ownAccount: boolean
  scopeServices: Record<string, string>
  /** The scopes the picked events need, and the chosen target kind's. */
  eventScopes: string[]
  kindScopes: string[]
}

/** The line above the events when the account lacks a scope they need, and
 * the create's refusal in the same words: which permission (a URL-shaped
 * scope by its last segment), and which service to tick when reconnecting. */
function scopeNotice(missing: string[], ctx: ScopeContext): string {
  // One order on the line and in the refusal: the server sorts its list.
  const scopes = [...new Set(missing)].sort()
  const name = (s: string) => s.split('/').pop() || s
  const noun = scopes.length === 1 ? 'permission' : 'permissions'
  const fromEvents = scopes.some((s) => ctx.eventScopes.includes(s) || !ctx.kindScopes.includes(s))
  const subject = fromEvents ? 'These events need' : 'This subscription needs'
  const need = `${subject} the ${joinAnd(scopes.map(name))} ${noun}.`
  // A scope no service asks for: no reconnect can grant it.
  const known = Object.keys(ctx.scopeServices).length > 0
  const ungranted = known ? scopes.filter((s) => !ctx.scopeServices[s]) : []
  if (ungranted.length) {
    return `${need} No service of this MCP grants ${joinAnd(ungranted.map(name))}, so its author needs to fix this.`
  }
  const labels = [...new Set(scopes.map((s) => ctx.scopeServices[s]).filter(Boolean))]
  const tick = labels.length ? ` and tick ${joinAnd(labels)}` : ''
  return ctx.ownAccount
    ? `${need} Reconnect the account${tick}.`
    : `${need} The account's owner must reconnect it${tick}.`
}

export function SubscribeToEventsModal({
  mcpName,
  accountLabel,
  providerId,
  eventCatalog,
  vendorTargetSpec,
  registrationMode = 'manual',
  manualInstructionsUrl,
  vendorTargetPrefill,
  installationTarget = false,
  scope = 'user',
  agent,
  serviceOptions,
  grantedScopes,
  scopeServices = {},
  onClose,
}: Props) {
  const create = useCreateSubscription()
  const [vendorTarget, setVendorTarget] = useState(
    vendorTargetSpec.kind === 'static_list' &&
      vendorTargetSpec.static_options?.length
      ? vendorTargetSpec.static_options[0].value
      : vendorTargetPrefill ?? '',
  )
  const [selectedEvents, setSelectedEvents] = useState<Set<string>>(
    new Set(
      eventCatalog
        .filter((e) => e.default_selected)
        .map((e) => e.key),
    ),
  )
  const [error, setError] = useState<string | null>(null)
  const kinds = vendorTargetSpec.target_kinds ?? []
  const [kindKey, setKindKey] = useState(kinds[0]?.key ?? '')
  const kind = kinds.find((k) => k.key === kindKey)
  // What the field shows and checks: the kind's fields over the flat spec.
  const fieldSpec: VendorTargetSpec = kind
    ? {
        ...vendorTargetSpec,
        label: kind.label,
        placeholder: kind.placeholder,
        validation_regex: kind.validation_regex,
        help_text: kind.help_text,
      }
    : vendorTargetSpec
  // A personal-only agent cannot hold agent-scope subscriptions: it is not
  // offered. A binding the caller does not manage stays visible but disabled
  // (the binding lends the token, the role decides who may subscribe).
  const options = (serviceOptions ?? []).filter((o) => o.agent_scope_available)
  const fixedAgent = scope === 'service' ? agent : undefined
  const [target, setTarget] = useState<string>(fixedAgent ?? PERSONAL)
  const chosenScope: 'user' | 'service' = target === PERSONAL ? 'user' : 'service'
  const chosenAgent = target === PERSONAL ? undefined : target

  // MS-Graph-style vendors pair each catalog event 1:1 with a vendor
  // resource (mail_inbox ↔ me/.../messages, calendar_events ↔ me/events)
  // and the vendor registers ONE subscription per resource. When EVERY
  // entry declares `resource_contains`, the resource picker disappears:
  // the user just ticks events, and submit creates one subscription per
  // selected event with its paired resource resolved automatically.
  // A pick list with nothing to pick (no installation owned yet, no
  // installation bound to the agent): the spec's help_text says what to do
  // first, and nothing can be created.
  const nothingToPick =
    vendorTargetSpec.kind === 'static_list' && !vendorTargetSpec.static_options?.length

  const fullyPaired =
    vendorTargetSpec.kind === 'static_list' &&
    !!vendorTargetSpec.static_options?.length &&
    eventCatalog.length > 0 &&
    eventCatalog.every((e) => !!e.resource_contains)

  const pairedTargetFor = (
    entry: WebhookEventCatalogEntry,
  ): string | null => {
    if (!entry.resource_contains) return null
    const needle = entry.resource_contains.toLowerCase()
    const opt = vendorTargetSpec.static_options?.find((o) =>
      o.value.toLowerCase().includes(needle),
    )
    return opt?.value ?? null
  }

  /**
   * Normalize a vendor target before validation. Trims whitespace, strips
   * common URL prefixes (`https://github.com/`, `https://`, `http://`),
   * and per-provider suffixes (`.git` for GitHub clone URLs). Users
   * routinely paste the full clone URL or browser-bar URL.
   */
  const normalizeVendorTarget = (raw: string): string => {
    let v = raw.trim()
    // Strip a leading URL host: turn `https://github.com/owner/repo` →
    // `owner/repo`. Works for any vendor-target spec.
    v = v.replace(/^https?:\/\/[^/]+\//, '')
    // GitHub-specific clone-URL suffix.
    if (providerId === 'github' && v.endsWith('.git')) v = v.slice(0, -4)
    return v
  }

  const validateVendorTarget = (normalized: string): string | null => {
    if (!normalized) return 'Vendor target is required'
    if (
      fieldSpec.validation_regex &&
      !new RegExp(fieldSpec.validation_regex).test(normalized)
    ) {
      const hint = fieldSpec.placeholder
        ? ` Expected: ${fieldSpec.placeholder}`
        : ''
      return `'${normalized}' doesn't look right.${hint}`
    }
    return null
  }

  // The scope, agent and account are decided once; every request of a
  // paired loop carries the same ones.
  const requestFor = (
    vendor_target: string,
    selected_events: string[],
  ): CreateSubscriptionRequest => ({
    scope: chosenScope,
    agent: chosenAgent,
    mcp_name: mcpName,
    account_label: accountLabel,
    vendor_target,
    selected_events,
    // The first kind is the default and travels as '' — the row convention
    // for every subscription made before kinds existed — so only the other
    // kinds carry a name (and a badge).
    ...(kinds.length ? { vendor_target_kind: kindKey === kinds[0].key ? '' : kindKey } : {}),
  })

  // The scopes the account lacks for what is picked: the ticked events',
  // or before a tick the ones every listed event needs, with the chosen
  // kind's. Unknown scopes (a token, an installation) say nothing.
  const scopesOf = (e: WebhookEventCatalogEntry) => e.required_scopes ?? []
  const ticked = eventCatalog.filter((e) => selectedEvents.has(e.key))
  const neededByEvents = ticked.length
    ? ticked.flatMap(scopesOf)
    : (eventCatalog.length ? scopesOf(eventCatalog[0]) : []).filter((s) =>
        eventCatalog.every((e) => scopesOf(e).includes(s)),
      )
  const granted = new Set(grantedScopes ?? [])
  const missingScopes = grantedScopes
    ? [...new Set([...neededByEvents, ...(kind?.required_scopes ?? [])])].filter(
        (s) => !granted.has(s),
      )
    : []

  const scopeContext: ScopeContext = {
    ownAccount: !fixedAgent, scopeServices,
    eventScopes: neededByEvents, kindScopes: kind?.required_scopes ?? [],
  }
  const describe = (err: unknown) => describeSubscriptionError(err, scopeContext)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    if (selectedEvents.size === 0) {
      setError('Select at least one event type')
      return
    }

    if (fullyPaired) {
      // One subscription per selected event, each with its paired
      // resource. Sequential so a vendor rejection stops the run with a
      // clear per-event error; a row that already exists (409, a re-submit
      // after a partial failure) is skipped so the rest still land.
      const entries = eventCatalog.filter((en) => selectedEvents.has(en.key))
      for (const entry of entries) {
        const target = pairedTargetFor(entry)
        if (!target) {
          setError(
            `The event ${entry.label} has no matching resource. The MCP's author needs to fix this.`,
          )
          return
        }
        try {
          await create.mutateAsync(requestFor(target, [entry.key]))
        } catch (err) {
          if (err instanceof SubscriptionRequestError && err.status === 409) continue
          setError(`${entry.label}: ${describe(err)}`)
          return
        }
      }
      onClose()
      return
    }

    const normalized = normalizeVendorTarget(vendorTarget)
    const validationError = validateVendorTarget(normalized)
    if (validationError) {
      setError(validationError)
      return
    }
    create.mutate(requestFor(normalized, Array.from(selectedEvents)), {
      onSuccess: () => onClose(),
      onError: (err) => setError(describe(err)),
    })
  }

  const toggleEvent = (key: string) => {
    setSelectedEvents((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <Dialog
      onClose={onClose}
      busy={create.isPending}
      aria-label={`Subscribe to ${providerId} events`}
      panelClassName={`${DIALOG_PANEL} max-w-lg max-h-[90vh] overflow-y-auto`}
    >
        <form onSubmit={handleSubmit}>
          <header className="px-5 py-4 border-b border-subtle">
            <h2 className="font-display text-lg font-semibold">
              Subscribe to {providerId} events
            </h2>
            <p className="text-sm text-tertiary mt-1">
              Account: <span className="font-mono">{accountLabel}</span>
            </p>
            {fixedAgent && (
              <p className="text-xs text-tertiary mt-1">
                For agent <span className="font-medium">{fixedAgent}</span>, with
                the account set as its service account. The agent&apos;s shared
                triggers and apps fire on these events.
              </p>
            )}
            {registrationMode === 'relay' && !installationTarget && (
              <p className="text-xs text-status-info-fg mt-1">
                Events come through OtoDock. There is nothing to set up
                anywhere else.
              </p>
            )}
            {registrationMode === 'manual' && (
              <p className="text-xs text-tertiary mt-1">
                After creating, paste the webhook URL shown on the
                subscription into the vendor console.
                {manualInstructionsUrl && (
                  <>
                    {' '}
                    <a
                      href={safeHref(manualInstructionsUrl)}
                      target="_blank"
                      rel="noreferrer"
                      className="text-link hover:underline"
                    >
                      Vendor instructions
                    </a>
                  </>
                )}
              </p>
            )}
          </header>

          <div className="px-5 py-4 space-y-4">
            {!fixedAgent && options.length > 0 && (
              <div>
                <label
                  htmlFor="subscribe-as"
                  className="block text-sm font-medium mb-1"
                >
                  Subscribe as
                </label>
                <Select
                  id="subscribe-as"
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                >
                  <option value={PERSONAL}>Me, personal</option>
                  {options.map((o) => (
                    <option
                      key={o.agent_name}
                      value={o.agent_name}
                      disabled={!o.can_manage}
                    >
                      Agent {o.display_name}
                      {o.can_manage ? '' : ', managers only'}
                    </option>
                  ))}
                </Select>
                <div className="text-xs text-tertiary mt-1">
                  {chosenScope === 'service'
                    ? "An agent's subscription fires the agent's shared triggers and apps."
                    : 'A personal subscription fires only your own triggers and apps. Pick an agent when a shared trigger or app should react.'}
                </div>
              </div>
            )}

            {!fullyPaired && kinds.length > 1 && (
              <div role="radiogroup" aria-label="Subscribe to" className="flex flex-wrap gap-1.5">
                {kinds.map((k) => (
                  <button
                    key={k.key}
                    type="button"
                    role="radio"
                    aria-checked={k.key === kindKey}
                    onClick={() => {
                      setKindKey(k.key)
                      setVendorTarget('')
                      setError(null)
                    }}
                    className={`px-2.5 py-1 rounded-full border text-xs ${
                      k.key === kindKey
                        ? 'bg-action text-action-fg border-action'
                        : 'bg-surface-card border-subtle text-secondary hover:text-primary'
                    }`}
                  >
                    {k.label}
                  </button>
                ))}
              </div>
            )}
            {!fullyPaired && (
              <VendorTargetField
                spec={fieldSpec}
                value={vendorTarget}
                onChange={setVendorTarget}
              />
            )}

            <div>
              <div className="mb-2">
                <label className="text-sm font-medium">
                  Event types
                </label>
                {fullyPaired && (
                  <InfoTip label="Event types">
                    Each selected event subscribes to its vendor resource on
                    its own. One subscription is created for each event.
                  </InfoTip>
                )}
              </div>
              {missingScopes.length > 0 && (
                <p className="text-xs text-status-warning-fg mb-2" data-testid="scope-notice">
                  {scopeNotice(missingScopes, scopeContext)}
                </p>
              )}
              <ul className="space-y-2 bg-surface-sunken rounded-card p-3 max-h-72 overflow-y-auto">
                {eventCatalog.map((entry) => (
                  <li key={entry.key} className="relative flex items-start gap-2">
                    <input
                      id={`subscribe-event-${entry.key}`}
                      type="checkbox"
                      checked={selectedEvents.has(entry.key)}
                      onChange={() => toggleEvent(entry.key)}
                      className="mt-1 accent-action cursor-pointer"
                    />
                    <div className="flex-1 text-sm font-medium">
                      <label htmlFor={`subscribe-event-${entry.key}`} className="cursor-pointer after:absolute after:inset-0">
                        {entry.label}
                      </label>
                      {entry.description && (
                        <span className="relative z-10">
                          <InfoTip label={entry.label}>{entry.description}</InfoTip>
                        </span>
                      )}
                      {entry.delivery === 'bot' && (
                        <span className={`${BADGE_BASE} ${BADGE_TONE.warning} ml-1.5 align-middle`}>
                          bot
                        </span>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </div>

            {error && (
              <div className="text-sm text-status-danger-fg bg-status-danger-bg border border-status-danger-border rounded-card p-2 whitespace-pre-wrap">
                {error}
              </div>
            )}
          </div>

          <footer className="px-5 py-4 border-t border-subtle flex justify-end gap-2">
            <Button variant="ghost" size="md"
              type="button"
              onClick={onClose}
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              size="md"
              type="submit"
              disabled={create.isPending || nothingToPick}
            >
              {create.isPending ? 'Creating…' : 'Create subscription'}
            </Button>
          </footer>
        </form>
    </Dialog>
  )
}

function VendorTargetField({
  spec,
  value,
  onChange,
}: {
  spec: VendorTargetSpec
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div>
      <label className="block text-sm font-medium mb-1">{spec.label}</label>
      {spec.kind === 'free_text' && (
        <Input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={spec.placeholder}
          /* Intentionally no HTML5 `pattern` — submit handler normalizes
             URL/`.git` paste sloppiness before regex validation. */
        />
      )}
      {spec.kind === 'static_list' && spec.static_options && (
        <Select
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          {spec.static_options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </Select>
      )}
      {spec.kind === 'remote_list' && (
        <Input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="A picker is coming soon. For now, paste the ID from the vendor."
        />
      )}
      {spec.help_text && (
        <div className="text-xs text-tertiary mt-1">{spec.help_text}</div>
      )}
    </div>
  )
}
