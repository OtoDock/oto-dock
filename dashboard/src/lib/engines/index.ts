/**
 * Engine helpers — every question a page asks about an AI engine, answered
 * from its descriptor (`EngineDescriptor`, the mirror of the proxy's
 * `LayerCapabilities.to_dict()`), never from the engine id. A fourth engine
 * that declares itself renders correctly everywhere without a dashboard edit.
 *
 * Pure functions; unit-tested in tests/engines.test.ts on descriptors whose
 * ids are not the real engines', so the tests prove the descriptor is read.
 * The effort ladder helpers are the sibling ./effort.ts.
 */
import type { EngineDescriptor, EngineOAuthFlow, EngineProvider } from '../../api/engineDescriptor'

/** The engine's name without its vendor: "OpenAI Codex" → "Codex". A name
 *  that does not start with the vendor ("Claude Code CLI", "Direct LLM API")
 *  is returned as is. */
export function engineLabel(d: EngineDescriptor): string {
  const prefix = d.identity.vendor_label ? `${d.identity.vendor_label} ` : ''
  return prefix && d.display_name.startsWith(prefix)
    ? d.display_name.slice(prefix.length)
    : d.display_name
}

/** The vendor chip shown before the engine's name — null for a
 *  multi-provider engine, which has no single vendor. */
export function vendorBadge(d: EngineDescriptor): string | null {
  return d.identity.vendor_label || null
}

/** What a subscription to the engine is called ("Claude", "ChatGPT"); ""
 *  for an engine without a login. */
export function accountLabel(d: EngineDescriptor): string {
  return d.identity.account_label
}

/** The AI Engines page order: `identity.sort_order`, then the id. */
export function sortEngines<T extends EngineDescriptor>(list: T[]): T[] {
  return [...list].sort(
    (a, b) => a.identity.sort_order - b.identity.sort_order || a.name.localeCompare(b.name),
  )
}

/** Rows that carry a descriptor (the admin and user endpoints' rows), in
 *  engine order. */
export function sortEngineRows<T extends { capabilities: EngineDescriptor }>(rows: T[]): T[] {
  return [...rows].sort(
    (a, b) =>
      a.capabilities.identity.sort_order - b.capabilities.identity.sort_order
      || a.capabilities.name.localeCompare(b.capabilities.name),
  )
}

/** The catalog (`useExecutionLayers()`), in engine order; [] until it loads. */
export function orderedEngines<T extends EngineDescriptor>(layers: Record<string, T> | undefined): T[] {
  return layers ? sortEngines(Object.values(layers)) : []
}

/** A coding engine — the ones the setup banners count and the auto-pick for
 *  a new agent chooses among. A supporting engine is the low-latency path
 *  (title generation, the phone classifier), not a coding agent. */
export function isCoding(d: EngineDescriptor): boolean {
  return d.identity.role === 'coding'
}

/** The engine takes a vendor login (an OAuth account). */
export function supportsOAuth(d: EngineDescriptor): boolean {
  return d.auth.auth_types.includes('oauth')
}

/** Which connect box the page renders for the engine's login. */
export function oauthFlow(d: EngineDescriptor): EngineOAuthFlow {
  return d.auth.oauth_flow
}

/** A self-hosted OpenAI-compatible endpoint may serve this engine. */
export function acceptsLocalEndpoints(d: EngineDescriptor): boolean {
  return d.auth.auth_types.includes('local_endpoint')
}

/** The hosted OtoDock relay may serve this engine. */
export function acceptsRelay(d: EngineDescriptor): boolean {
  return d.auth.auth_types.includes('relay')
}

/** The engine's `providers[]` entry for `id`, or undefined when it declares
 *  none (a local provider on hosted OtoDock, another engine's vendor). */
export function providerEntry(d: EngineDescriptor | undefined, id: string): EngineProvider | undefined {
  return d?.providers?.find((p) => p.id === id)
}

/** The descriptor's provider kinds (`PROVIDER_KINDS` on the proxy). */
export const PROVIDER_LOCAL = 'local'

/** A self-hosted provider (the descriptor's `local` kind): its models exist
 *  only while an endpoint row is active. */
export function isLocalProvider(d: EngineDescriptor | undefined, id: string): boolean {
  return providerEntry(d, id)?.kind === PROVIDER_LOCAL
}

/** The providers the hosted relay fronts on this engine — the entries that
 *  declare a `relay_path`. */
export function relayProviders(d: EngineDescriptor): EngineProvider[] {
  return (d.providers ?? []).filter((p) => !!p.relay_path)
}

/** The provider's label from the entries at hand, else its id. */
export function providerLabel(providers: EngineProvider[] | null | undefined, id: string): string {
  return providers?.find((p) => p.id === id)?.label || id
}

/** `installed_clis` name → binary, for every engine with a CLI: what the
 *  machine cards need to read `cli_status` (keyed by binary) for a chip. */
export function installedCliBinaries(layers: Record<string, EngineDescriptor> | undefined): Record<string, string> {
  const out: Record<string, string> = {}
  for (const d of Object.values(layers ?? {})) {
    if (d.runtime.installed_name && d.runtime.binary) out[d.runtime.installed_name] = d.runtime.binary
  }
  return out
}

/** The providers an API key on this engine may belong to: the key-taking
 *  entries of `providers` (a local endpoint is not a key), or the engine's
 *  own vendor when it declares no provider list; [] for an engine that takes
 *  no key at all. One entry → the form shows no provider select. */
export function keyProviders(d: EngineDescriptor): EngineProvider[] {
  if (!d.auth.auth_types.includes('api_key')) return []
  if (d.providers) return d.providers.filter((p) => p.requires_key !== false)
  return d.identity.vendor_id
    ? [{ id: d.identity.vendor_id, label: d.identity.vendor_label || d.identity.vendor_id, requires_key: true }]
    : []
}

/** The engine has a native TUI the platform spawns under a PTY. Unknown
 *  (the catalog has not loaded) → false: the interactive toggle stays
 *  hidden until the descriptor says otherwise. */
export function runsInteractive(d: EngineDescriptor | undefined): boolean {
  return d?.runtime.supports_interactive_pty ?? false
}

/** The engine can run on a satellite. Unknown → true: a select or an
 *  eligibility list stays open until the descriptor says otherwise (the
 *  server refuses a wrong assignment at start). */
export function runsRemote(d: EngineDescriptor | undefined): boolean {
  return d?.runtime.supports_remote_execution ?? true
}

/** The engine compacts a conversation on request (the composer's Compact
 *  button). Unknown engine or no catalog → false: the button stays hidden
 *  until the descriptor says otherwise. */
export function supportsCompact(layers: Record<string, EngineDescriptor> | undefined, id: string): boolean {
  return layers?.[id]?.behaviour.supports_compact ?? false
}

/** id → label for every engine in the catalog; {} until it loads. */
export function engineLabels(layers: Record<string, EngineDescriptor> | undefined): Record<string, string> {
  const out: Record<string, string> = {}
  for (const d of Object.values(layers ?? {})) out[d.name] = engineLabel(d)
  return out
}

/** The coding engines' labels, in engine order — what the setup banners name. */
export function codingEngineNames(engines: EngineDescriptor[]): string[] {
  return sortEngines(engines).filter(isCoding).map(engineLabel)
}

/** "A", "A or B", "A, B or C" — for a sentence that names the engines. */
export function joinOr(names: string[]): string {
  if (names.length <= 1) return names[0] ?? ''
  return `${names.slice(0, -1).join(', ')} or ${names[names.length - 1]}`
}
