/**
 * Capability tiers as the dashboard shows them: a four-dot mark, more dots
 * for a more capable model, the tier word and the model's "good at" line in
 * the tooltip. The tiers themselves (1 = frontier … 4 = fast) come from the
 * proxy (`tier` / `tier_label` / `good_at` on `GET /v1/execution-layers`,
 * `effective_model_tier` / `tier_label` on tasks and triggers); nothing here
 * decides a tier, it only renders one.
 */

export const TIER_LABELS: Record<number, string> = {
  1: 'frontier', 2: 'strong', 3: 'balanced', 4: 'fast',
}

export const TIER_COUNT = 4

function isTier(tier: number | null | undefined): tier is number {
  return typeof tier === 'number' && tier >= 1 && tier <= TIER_COUNT
}

/** Filled dots for a tier: frontier 4 … fast 1; 0 when untiered. */
export function tierDots(tier: number | null | undefined): number {
  return isTier(tier) ? TIER_COUNT + 1 - tier : 0
}

/** The mark as text, for places that can only show a string (a native
 * `<option>`): "●●●●" for frontier, "●○○○" for fast, "" when untiered. */
export function tierMarkText(tier: number | null | undefined): string {
  const filled = tierDots(tier)
  if (!filled) return ''
  return '●'.repeat(filled) + '○'.repeat(TIER_COUNT - filled)
}

/** "tier 2 strong" for a tiered model, "untiered" otherwise. */
export function tierText(tier: number | null | undefined, label?: string): string {
  if (!isTier(tier)) return 'untiered'
  return `tier ${tier} ${label || TIER_LABELS[tier] || ''}`.trim()
}

/** The tooltip behind a mark: "Strong (tier 2 of 4): complex coding and
 * analysis" or "Untiered: no capability rating yet". */
export function tierTitle(
  tier: number | null | undefined, label?: string, goodAt?: string,
): string {
  if (!isTier(tier)) return 'Untiered: no capability rating yet'
  const word = label || TIER_LABELS[tier] || `tier ${tier}`
  const head = `${word.charAt(0).toUpperCase()}${word.slice(1)} (tier ${tier} of ${TIER_COUNT})`
  return goodAt ? `${head}: ${goodAt}` : head
}
