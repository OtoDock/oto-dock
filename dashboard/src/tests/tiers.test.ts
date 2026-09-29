import { describe, it, expect } from 'vitest'

import { tierDots, tierMarkText, tierText, tierTitle } from '@/lib/tiers'

// ─── The capability mark: four dots, more for a more capable model ─────────

describe('tiers', () => {
  it('fills the dots from frontier (4) to fast (1), none when untiered', () => {
    expect([1, 2, 3, 4].map(tierDots)).toEqual([4, 3, 2, 1])
    expect(tierDots(null)).toBe(0)
    expect(tierDots(undefined)).toBe(0)
    expect(tierDots(0)).toBe(0)
    expect(tierDots(9)).toBe(0)
  })

  it('renders the text form for native option rows', () => {
    expect(tierMarkText(1)).toBe('●●●●')
    expect(tierMarkText(3)).toBe('●●○○')
    expect(tierMarkText(4)).toBe('●○○○')
    expect(tierMarkText(null)).toBe('')
  })

  it('writes the tooltip with the word, the rank and the good-at line', () => {
    expect(tierTitle(2, 'strong', 'complex coding')).toBe('Strong (tier 2 of 4): complex coding')
    expect(tierTitle(4)).toBe('Fast (tier 4 of 4)')
    expect(tierTitle(null, undefined, 'local')).toBe('Untiered: no capability rating yet')
  })

  it('keeps the short text form for chip tooltips', () => {
    expect(tierText(2, 'strong')).toBe('tier 2 strong')
    expect(tierText(1)).toBe('tier 1 frontier')
    expect(tierText(null)).toBe('untiered')
  })
})
