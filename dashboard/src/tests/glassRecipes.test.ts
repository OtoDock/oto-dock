import { describe, expect, it } from 'vitest'

import { GLASS, GLASS_FILL, GLASS_HOVER, GLASS_PRESSED } from '@/components/ui/glass'

// The glass recipes are one recipe in three states: each differs from GLASS
// only where its name says, so the chat chrome's controls cannot drift apart.

const words = (s: string) => s.split(/\s+/).filter(Boolean)
const without = (s: string, drop: string[]) => words(s).filter((w) => !drop.includes(w))

describe('the glass recipes', () => {
  it('pressed is the rest recipe with the hover fill', () => {
    expect(words(GLASS_PRESSED)).toEqual(words(GLASS).map((w) => (w === 'bg-glass-fill' ? 'bg-glass-hover' : w)))
    expect(GLASS_HOVER).toBe('hover:bg-glass-hover')
  })

  it('the fill is the rest recipe without its edge', () => {
    expect(words(GLASS_FILL)).toEqual(without(GLASS, ['border', 'border-glass-border']))
    expect(GLASS_FILL).not.toMatch(/\bborder\b/)
  })

  it('every recipe keeps the blur, the fill family and one elevation', () => {
    for (const r of [GLASS, GLASS_PRESSED, GLASS_FILL]) {
      expect(words(r)).toContain('backdrop-blur-xs')
      expect(words(r).filter((w) => w.startsWith('bg-glass-'))).toHaveLength(1)
      expect(words(r).filter((w) => w.startsWith('shadow-elevation-'))).toHaveLength(1)
    }
  })
})
