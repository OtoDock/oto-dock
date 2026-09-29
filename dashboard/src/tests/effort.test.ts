import { describe, it, expect } from 'vitest'

// ─── lib/effort: the Default Effort ladder, the per-level gates and the
//     coerce rule are answered from engine descriptors and their providers[]
//     entries. The engines and providers here are invented so a helper that
//     remembered the real ladders or provider names would fail. ────────────

import {
  coerceEffort, effortLabel, effortLadder, offeredEffortLevels, perModelLevels, providerEntry,
} from '@/lib/engines/effort'
import { descriptor } from './fixtures/engines'

const LADDER = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']

// A vendor whose API keeps xhigh and max apart and rejects xhigh on older
// models (the row flag decides), plus a top-only "turbo" level.
const forge = descriptor({
  name: 'forge-cli',
  effort_levels: LADDER,
  providers: [
    { id: 'forge', label: 'Forge', requires_key: true,
      effort_scale: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], effort_per_model: ['xhigh', 'ultra'] },
    { id: 'lan_box', label: 'A box on the LAN', requires_key: false,
      effort_scale: ['low', 'medium', 'high', 'xhigh'], effort_per_model: [] },
    { id: 'mist', label: 'Mist', requires_key: true,
      effort_scale: ['low', 'medium', 'high'], effort_per_model: [] },
  ],
})
// A supporting engine with a shorter ladder and no declarations at all.
const quill = descriptor({ name: 'quill-api', effort_levels: ['low', 'high', 'max'] })

const m = (over: Record<string, unknown>) => ({ value: 'm', label: 'M', ...over })

describe('effortLadder', () => {
  it('merges the enabled ladders in order, an unseen level after its predecessor', () => {
    expect(effortLadder([forge])).toEqual(LADDER)
    expect(effortLadder([quill, forge])).toEqual(['low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
    expect(effortLadder([forge, quill])).toEqual(LADDER)
    const odd = descriptor({ effort_levels: ['low', 'brisk', 'max'] })
    expect(effortLadder([quill, odd])).toEqual(['low', 'brisk', 'high', 'max'])
    expect(effortLadder([])).toEqual([])
  })
})

describe('effortLabel', () => {
  it('labels the platform vocabulary and capitalizes anything else', () => {
    expect(effortLabel('xhigh')).toBe('XHigh')
    expect(effortLabel('ultra')).toBe('Ultra')
    expect(effortLabel('brisk')).toBe('Brisk')
  })
})

describe('providerEntry / perModelLevels', () => {
  it('finds the declared entry and falls back to the two row flags', () => {
    expect(providerEntry(forge, 'lan_box')?.label).toBe('A box on the LAN')
    expect(providerEntry(forge, 'nope')).toBeUndefined()
    expect(providerEntry(quill, 'forge')).toBeUndefined()
    expect(perModelLevels(forge, 'forge')).toEqual(['xhigh', 'ultra'])
    expect(perModelLevels(forge, 'lan_box')).toEqual([])
    expect(perModelLevels(quill, 'anything')).toEqual(['xhigh', 'ultra'])
  })
})

describe('offeredEffortLevels', () => {
  it('trims the ladder to the provider scale and gates per-model levels by the row', () => {
    const flagged = m({ provider: 'forge', supports_xhigh: true, supports_ultra: true })
    const plain = m({ provider: 'forge' })
    expect(offeredEffortLevels(LADDER, [{ engine: forge, model: flagged }], [forge])).toEqual(LADDER)
    expect(offeredEffortLevels(LADDER, [{ engine: forge, model: plain }], [forge]))
      .toEqual(['low', 'medium', 'high', 'max'])
  })

  it('a provider whose scale tops at xhigh offers no Max; a three-level one no XHigh', () => {
    const lan = m({ provider: 'lan_box' })
    expect(offeredEffortLevels(LADDER, [{ engine: forge, model: lan }], [forge]))
      .toEqual(['low', 'medium', 'high', 'xhigh'])
    const mist = m({ provider: 'mist', supports_xhigh: true })
    expect(offeredEffortLevels(LADDER, [{ engine: forge, model: mist }], [forge]))
      .toEqual(['low', 'medium', 'high'])
  })

  it('a row whose provider the engine does not declare keeps the engine ladder and the row flags', () => {
    const row = m({ provider: 'unknown', supports_xhigh: true })
    expect(offeredEffortLevels(LADDER, [{ engine: quill, model: row }], [quill]))
      .toEqual(['low', 'high', 'max'])
    const bare = m({ provider: 'unknown' })
    const withXhigh = descriptor({ name: 'bare-cli', effort_levels: ['low', 'xhigh', 'max'] })
    expect(offeredEffortLevels(LADDER, [{ engine: withXhigh, model: bare }], [withXhigh]))
      .toEqual(['low', 'max'])
  })

  it('a model listed under two engines is offered what either offers', () => {
    const onForge = { engine: forge, model: m({ provider: 'forge', supports_ultra: true }) }
    const onQuill = { engine: quill, model: m({ provider: 'unknown' }) }
    expect(offeredEffortLevels(LADDER, [onForge, onQuill], [forge, quill]))
      .toEqual(['low', 'medium', 'high', 'max', 'ultra'])
  })

  it('with no listing, withholds every level some enabled engine gates per model', () => {
    expect(offeredEffortLevels(LADDER, [], [forge])).toEqual(['low', 'medium', 'high', 'max'])
    const lanOnly = descriptor({
      name: 'lan-cli', effort_levels: LADDER,
      providers: [{ id: 'lan_box', label: 'LAN', effort_scale: ['low', 'high', 'xhigh'], effort_per_model: [] }],
    })
    expect(offeredEffortLevels(LADDER, [], [lanOnly])).toEqual(LADDER)
    // An engine without declarations gates the two row flags.
    expect(offeredEffortLevels(LADDER, [], [quill])).toEqual(['low', 'medium', 'high', 'max'])
  })
})

describe('coerceEffort', () => {
  const offered = ['low', 'medium', 'high', 'max']
  it('keeps an offered value and the empty default', () => {
    expect(coerceEffort('', offered, LADDER)).toBe('')
    expect(coerceEffort('max', offered, LADDER)).toBe('max')
  })
  it('falls to the highest offered level below the stored one', () => {
    expect(coerceEffort('xhigh', offered, LADDER)).toBe('high')
    expect(coerceEffort('ultra', offered, LADDER)).toBe('max')
    expect(coerceEffort('max', ['low', 'medium', 'high', 'xhigh'], LADDER)).toBe('xhigh')
  })
  it('a platform level no enabled engine lists ranks by the platform order', () => {
    // A Codex `ultra` on an agent now Claude-only: it runs as max there.
    const claude = ['low', 'medium', 'high', 'xhigh', 'max']
    expect(coerceEffort('ultra', claude, claude)).toBe('max')
    expect(coerceEffort('ultra', ['low', 'medium', 'high'], ['low', 'medium', 'high'])).toBe('high')
  })
  it('a value outside the ladder, or with nothing below, lands on high or the first offered', () => {
    expect(coerceEffort('minimal', offered, LADDER)).toBe('high')
    expect(coerceEffort('low', ['medium', 'high'], LADDER)).toBe('high')
    expect(coerceEffort('minimal', ['max', 'ultra'], LADDER)).toBe('max')
    expect(coerceEffort('xhigh', [], LADDER)).toBe('')
  })
})
