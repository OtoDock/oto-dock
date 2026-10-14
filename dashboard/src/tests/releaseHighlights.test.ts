// The bundled highlights file stays honest: every version key is a release
// number, every audience is one of the two, at most six cards each, every
// icon one the dialog draws, a title at most 40 characters and a text at most
// 160, nothing empty; a release without an entry has no cards.

import { describe, it, expect } from 'vitest'
import data from '@/release/highlights.json'
import { HIGHLIGHT_LIMITS, ICON_NAMES, ICON_PATHS, VERSION_RE, highlightsFor } from '@/release/highlights'

const AUDIENCES = ['everyone', 'admins'] as const

describe('release/highlights.json', () => {
  it('every entry follows the rules', () => {
    for (const [version, entry] of Object.entries(data as Record<string, Record<string, unknown>>)) {
      expect(version, `version key ${version}`).toMatch(VERSION_RE)
      for (const audience of Object.keys(entry)) expect(AUDIENCES, `${version}: audience ${audience}`).toContain(audience)
      for (const audience of AUDIENCES) {
        const cards = (entry[audience] === undefined ? [] : entry[audience]) as Array<Record<string, string>>
        expect(Array.isArray(cards), `${version}.${audience} is a list`).toBe(true)
        expect(cards.length, `${version}.${audience} cards`).toBeLessThanOrEqual(HIGHLIGHT_LIMITS.cards)
        for (const card of cards) {
          const where = `${version}.${audience} "${card.title}"`
          expect(Object.keys(card).sort(), where).toEqual(['icon', 'text', 'title'])
          expect(ICON_NAMES, `${where}: icon ${card.icon}`).toContain(card.icon)
          expect(card.title.trim().length, `${where}: title`).toBeGreaterThan(0)
          expect(card.title.length, `${where}: title length`).toBeLessThanOrEqual(HIGHLIGHT_LIMITS.title)
          expect(card.text.trim().length, `${where}: text`).toBeGreaterThan(0)
          expect(card.text.length, `${where}: text length`).toBeLessThanOrEqual(HIGHLIGHT_LIMITS.text)
        }
      }
    }
  })

  it('every icon has a stroked path and a release without an entry has no cards', () => {
    for (const name of ICON_NAMES) expect(ICON_PATHS[name]).toMatch(/^M/)
    expect(highlightsFor('0.0.1')).toEqual({ everyone: [], admins: [] })
    expect(highlightsFor('1.8.1-rc1')).toEqual(highlightsFor('1.8.1'))
  })
})
