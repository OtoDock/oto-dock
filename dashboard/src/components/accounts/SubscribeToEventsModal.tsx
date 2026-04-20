/**
 * "Subscribe to events" modal.
 *
 * Three logical steps in one modal:
 *   1. Vendor target — driven by manifest's `vendor_target_spec.kind`
 *      (free_text + regex / static_list dropdown). remote_list pickers
 *      are deferred until the list endpoint lands.
 *   2. Event picker — checkbox list from manifest's `event_catalog`.
 *   3. Confirm — POST /v1/subscriptions; errors surface inline.
 */

import { useState } from 'react'
import {
  useCreateSubscription,
  type WebhookEventCatalogEntry,
  type VendorTargetSpec,
} from '../../api/subscriptions'

interface Props {
  mcpName: string
  accountLabel: string
  providerId: string
  eventCatalog: WebhookEventCatalogEntry[]
  vendorTargetSpec: VendorTargetSpec
  /** EFFECTIVE registration mode for this account (relay = hosted delivery,
   * zero vendor-console steps). */
  registrationMode?: 'relay' | 'auto' | 'manual'
  manualInstructionsUrl?: string
  /** Prefill for the vendor-target input (slack: the account's team_id). */
  vendorTargetPrefill?: string
  onClose: () => void
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

  // MS-Graph-style vendors pair each catalog event 1:1 with a vendor
  // resource (mail_inbox ↔ me/.../messages, calendar_events ↔ me/events)
  // and the vendor registers ONE subscription per resource. When EVERY
  // entry declares `resource_contains`, the resource picker disappears:
  // the user just ticks events, and submit creates one subscription per
  // selected event with its paired resource resolved automatically.
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
      vendorTargetSpec.validation_regex &&
      !new RegExp(vendorTargetSpec.validation_regex).test(normalized)
    ) {
      const hint = vendorTargetSpec.placeholder
        ? ` Expected: ${vendorTargetSpec.placeholder}`
        : ''
      return `'${normalized}' doesn't look right.${hint}`
    }
    return null
  }

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
      // clear per-event error (already-created ones stay and show in the
      // panel — re-submitting the rest is safe).
      const entries = eventCatalog.filter((en) => selectedEvents.has(en.key))
      for (const entry of entries) {
        const target = pairedTargetFor(entry)
        if (!target) {
          setError(
            `No resource option pairs with event '${entry.label}' — manifest bug`,
          )
          return
        }
        try {
          await create.mutateAsync({
            scope: 'user',
            mcp_name: mcpName,
            account_label: accountLabel,
            vendor_target: target,
            selected_events: [entry.key],
          })
        } catch (err) {
          setError(`${entry.label}: ${(err as Error).message}`)
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
    create.mutate(
      {
        scope: 'user',
        mcp_name: mcpName,
        account_label: accountLabel,
        vendor_target: normalized,
        selected_events: Array.from(selectedEvents),
      },
      {
        onSuccess: () => onClose(),
        onError: (e) => setError((e as Error).message),
      },
    )
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
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="bg-white dark:bg-gray-900 rounded-xl shadow-2xl max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto">
        <form onSubmit={handleSubmit}>
          <header className="px-5 py-4 border-b border-p-border-light">
            <h2 className="text-lg font-semibold">
              Subscribe to {providerId} events
            </h2>
            <p className="text-sm text-p-text-light mt-1">
              Account: <span className="font-mono">{accountLabel}</span>
            </p>
            {registrationMode === 'relay' && (
              <p className="text-xs text-purple-700 dark:text-purple-300 mt-1">
                Events are delivered through OtoDock — no vendor console
                steps needed.
              </p>
            )}
            {registrationMode === 'manual' && (
              <p className="text-xs text-p-text-light mt-1">
                After creating, paste the webhook URL shown on the
                subscription into the vendor console.
                {manualInstructionsUrl && (
                  <>
                    {' '}
                    <a
                      href={manualInstructionsUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="text-brand hover:underline"
                    >
                      Vendor instructions
                    </a>
                  </>
                )}
              </p>
            )}
          </header>

          <div className="px-5 py-4 space-y-4">
            {!fullyPaired && (
              <VendorTargetField
                spec={vendorTargetSpec}
                value={vendorTarget}
                onChange={setVendorTarget}
              />
            )}

            <div>
              <label className="block text-sm font-medium mb-2">
                Event types
              </label>
              {fullyPaired && (
                <p className="text-xs text-p-text-light mb-2">
                  Each selected event subscribes to its vendor resource
                  automatically — one subscription is created per event.
                </p>
              )}
              <ul className="space-y-2 border border-p-border-light rounded-sm p-3 max-h-72 overflow-y-auto">
                {eventCatalog.map((entry) => (
                  <li key={entry.key}>
                    <label className="flex items-start gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={selectedEvents.has(entry.key)}
                        onChange={() => toggleEvent(entry.key)}
                        className="mt-1"
                      />
                      <div className="flex-1">
                        <div className="text-sm font-medium">
                          {entry.label}
                          {entry.delivery === 'bot' && (
                            <span className="ml-1.5 inline-block px-1.5 rounded-sm text-[10px] uppercase font-semibold bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400 align-middle">
                              bot
                            </span>
                          )}
                        </div>
                        {entry.description && (
                          <div className="text-xs text-p-text-light">
                            {entry.description}
                          </div>
                        )}
                        {entry.required_scopes && entry.required_scopes.length > 0 && (
                          <div className="text-xs text-p-text-light mt-0.5">
                            Required scopes:{' '}
                            <span className="font-mono">
                              {entry.required_scopes.join(', ')}
                            </span>
                          </div>
                        )}
                      </div>
                    </label>
                  </li>
                ))}
              </ul>
            </div>

            {error && (
              <div className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-sm p-2 whitespace-pre-wrap">
                {error}
              </div>
            )}
          </div>

          <footer className="px-5 py-4 border-t border-p-border-light flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-p-text-light hover:bg-gray-100 dark:hover:bg-gray-800 rounded-sm"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={create.isPending}
              className="px-4 py-2 text-sm bg-brand hover:bg-brand-hover text-white rounded-sm disabled:opacity-50"
            >
              {create.isPending ? 'Creating…' : 'Create subscription'}
            </button>
          </footer>
        </form>
      </div>
    </div>
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
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={spec.placeholder}
          /* Intentionally no HTML5 `pattern` — submit handler normalizes
             URL/`.git` paste sloppiness before regex validation. */
          className="w-full px-3 py-2 border border-p-border-light rounded-sm text-sm bg-white dark:bg-gray-800"
        />
      )}
      {spec.kind === 'static_list' && spec.static_options && (
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full px-3 py-2 border border-p-border-light rounded-sm text-sm bg-white dark:bg-gray-800"
        >
          {spec.static_options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      )}
      {spec.kind === 'remote_list' && (
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Remote-list picker coming soon — paste the vendor-side ID for now"
          className="w-full px-3 py-2 border border-p-border-light rounded-sm text-sm bg-white dark:bg-gray-800"
        />
      )}
      {spec.help_text && (
        <div className="text-xs text-p-text-light mt-1">{spec.help_text}</div>
      )}
    </div>
  )
}
