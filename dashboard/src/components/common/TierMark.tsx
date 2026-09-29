import { TIER_COUNT, tierDots, tierTitle } from '../../lib/tiers'

/**
 * The capability mark next to a model name: four dots, filled from the left,
 * frontier ●●●● to fast ●○○○. Renders nothing for an untiered model (the
 * surrounding tooltip says so); the dots take the current text colour, so a
 * selected row's brand colour and a muted chip's grey both carry through.
 */
export function TierMark({ tier, label, goodAt, size = 'sm', className = '' }: {
  tier: number | null | undefined
  label?: string
  goodAt?: string
  size?: 'xs' | 'sm'
  className?: string
}) {
  const filled = tierDots(tier)
  if (!filled) return null
  const dot = size === 'xs' ? 'w-1 h-1' : 'w-1.5 h-1.5'
  const title = tierTitle(tier, label, goodAt)
  return (
    <span
      className={`inline-flex items-center gap-[2px] shrink-0 ${className}`}
      title={title}
      aria-label={title}
      role="img"
      data-tier={tier ?? undefined}
    >
      {Array.from({ length: TIER_COUNT }, (_, i) => (
        <span
          key={i}
          className={`${dot} rounded-full bg-current ${i < filled ? '' : 'opacity-25'}`}
        />
      ))}
    </span>
  )
}
