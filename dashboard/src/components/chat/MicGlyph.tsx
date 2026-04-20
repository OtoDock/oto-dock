// Shared microphone glyph — capsule body + pickup arc + stand, matching the
// icon set's 24px stroke-2 rounded style. `filled` solidifies the capsule
// (recording state); the arc and stand stay stroked in both variants.

export function MicGlyph({ className = 'w-5 h-5', filled = false }: {
  className?: string
  filled?: boolean
}) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="9" y="2" width="6" height="13" rx="3" fill={filled ? 'currentColor' : 'none'} />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <path d="M12 19v3" />
    </svg>
  )
}
