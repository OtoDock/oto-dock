/**
 * Effort helpers — the agent's Default Effort ladder and the admin xhigh
 * checkbox, answered from the engine descriptors (`effort_levels` and each
 * `providers[]` entry's `effort_scale` / `effort_per_model`), never from a
 * hard-coded list or a provider name.
 *
 * Pure functions; unit-tested in tests/effort.test.ts on invented engines
 * and providers.
 */
import type { EngineDescriptor, EngineProvider, LayerModelOption } from '../../api/engineDescriptor'

/** The platform vocabulary's labels — presentation, not identity: a level
 *  the table does not know is shown capitalized. */
const EFFORT_LABELS: Record<string, string> = {
  low: 'Low', medium: 'Medium', high: 'High', xhigh: 'XHigh', max: 'Max', ultra: 'Ultra',
}

export function effortLabel(level: string): string {
  return EFFORT_LABELS[level] ?? (level.charAt(0).toUpperCase() + level.slice(1))
}

/** The ordered union of the engines' ladders: each ladder is walked in
 *  order and an unseen level lands right after its predecessor from that
 *  ladder (deterministic for a given engine order). */
export function effortLadder(engines: EngineDescriptor[]): string[] {
  const out: string[] = []
  for (const e of engines) {
    let anchor = -1
    for (const level of e.effort_levels) {
      const at = out.indexOf(level)
      if (at >= 0) { anchor = at; continue }
      out.splice(anchor + 1, 0, level)
      anchor += 1
    }
  }
  return out
}

/** The engine's entry for a provider id, or undefined when it declares
 *  none or another id. */
export function providerEntry(engine: EngineDescriptor, providerId: string): EngineProvider | undefined {
  return engine.providers?.find((p) => p.id === providerId)
}

/** The two levels a model row can flag; an engine that declares no entry
 *  for the row's provider gates both by the row, as before the declaration. */
const ROW_FLAGGED_LEVELS = ['xhigh', 'ultra']

/** The levels of the provider's scale a model row's flag decides. */
export function perModelLevels(engine: EngineDescriptor, providerId: string): string[] {
  const entry = providerEntry(engine, providerId)
  return entry ? (entry.effort_per_model ?? []) : ROW_FLAGGED_LEVELS
}

function rowSupports(model: LayerModelOption, level: string): boolean {
  if (level === 'xhigh') return !!model.supports_xhigh
  if (level === 'ultra') return !!model.supports_ultra
  return false
}

/** One engine listing one model row. A model can sit under two engines. */
export interface EffortListing {
  engine: EngineDescriptor
  model: LayerModelOption
}

/** Which levels of `ladder` the Default Effort select offers. With
 *  listings, a level is offered when some listing offers it: it is in that
 *  engine's ladder and — when the engine declares the row's provider — in
 *  the entry's scale, and a per-model level only when the row's flag is
 *  set. Without a listing (Auto resolving nothing, a pin the list hides)
 *  every level some enabled engine gates per model is withheld. */
export function offeredEffortLevels(
  ladder: string[], listings: EffortListing[], enabled: EngineDescriptor[],
): string[] {
  if (listings.length === 0) {
    const gated = new Set<string>()
    for (const e of enabled) {
      const entries = e.providers?.length ? e.providers : undefined
      const perModel = entries
        ? entries.flatMap((p) => p.effort_per_model ?? [])
        : ROW_FLAGGED_LEVELS
      for (const lvl of perModel) gated.add(lvl)
    }
    return ladder.filter((lvl) => !gated.has(lvl))
  }
  return ladder.filter((lvl) => listings.some(({ engine, model }) => {
    if (!engine.effort_levels.includes(lvl)) return false
    const entry = providerEntry(engine, model.provider ?? '')
    if (entry && !(entry.effort_scale ?? []).includes(lvl)) return false
    return perModelLevels(engine, model.provider ?? '').includes(lvl) ? rowSupports(model, lvl) : true
  }))
}

/** The stored value the select can show: an offered value as is; else the
 *  highest offered level below it in the ladder (xhigh → high, ultra → max,
 *  max → xhigh where the scale tops at xhigh — the same wire value), a
 *  platform level no enabled engine lists ranked by the platform order (an
 *  `ultra` left by an engine the agent dropped runs as max, never high);
 *  else the highest offered level at or below high's position, else the
 *  first offered level. '' (High by default) stays ''. */
export function coerceEffort(stored: string, offered: string[], ladder: string[]): string {
  if (!stored || offered.includes(stored)) return stored
  if (offered.length === 0) return ''
  const inLadder = ladder.includes(stored)
  const order = inLadder || !(stored in EFFORT_LABELS) ? ladder : Object.keys(EFFORT_LABELS)
  const rank = (lvl: string) => order.indexOf(lvl)
  const below = (limit: number) => offered.filter((lvl) => rank(lvl) >= 0 && rank(lvl) < limit)
  const under = rank(stored) >= 0 ? below(rank(stored)) : []
  if (under.length) return under[under.length - 1]
  const upToHigh = ladder.indexOf('high') >= 0
    ? offered.filter((lvl) => ladder.indexOf(lvl) >= 0 && ladder.indexOf(lvl) <= ladder.indexOf('high'))
    : []
  return upToHigh.length ? upToHigh[upToHigh.length - 1] : offered[0]
}
