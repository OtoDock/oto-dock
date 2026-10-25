// @vitest-environment node
// Tailwind 4 drops a utility it does not know without a word, so a renamed
// or misspelled colour token would silently paint nothing. This compiles
// every colour-like class in src/ with Tailwind's own design system, loaded
// from index.css, and fails on any that does not compile.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { __unstable__loadDesignSystem } from '@tailwindcss/node'
import { beforeAll, describe, expect, it } from 'vitest'

import { DASHBOARD } from '../../design/build.mjs'

const SRC = join(DASHBOARD, 'src')

// Strings that look like a colour utility but are not classes.
const NOT_CLASSES = new Set([
  'transition:border-color', // an inline style string (agents map)
  'from-image', // createImageBitmap's imageOrientation option
])

const COLOUR_PREFIX =
  '(?:bg|text|border|border-[trblxyse]|ring|ring-offset|from|via|to|fill|stroke|outline|decoration|divide|placeholder|caret|accent|shadow|inset-shadow|drop-shadow)'
const CANDIDATE = new RegExp(
  `^(?:[a-z0-9-]+:|\\[[^\\]]+\\]:)*!?-?${COLOUR_PREFIX}-(?:[a-z][a-z0-9-]*|\\[[^\\]]+\\])(?:\\/[0-9]+|\\/\\[[^\\]]+\\])?!?$`,
)

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name)
    if (e.isDirectory()) {
      if (e.name !== 'tests') sourceFiles(p, out)
    } else if (/\.(ts|tsx)$/.test(e.name) && !/\.test\./.test(e.name) && !e.name.endsWith('.d.ts')) {
      out.push(p)
    }
  }
  return out
}

/** Every whitespace-separated word of every string literal in a file
 *  (comment lines dropped first: an apostrophe in prose is not a quote). */
function words(text: string): string[] {
  const out: string[] = []
  const code = text
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .split('\n')
    .filter((line) => !/^\s*\/\//.test(line))
    .join('\n')
  for (const m of code.matchAll(/'([^'\n]*)'|"([^"\n]*)"|`([^`]*)`/g)) {
    const s = (m[1] ?? m[2] ?? m[3] ?? '').replace(/\$\{[^}]*\}/g, ' ')
    out.push(...s.split(/\s+/))
  }
  return out
}

type DesignSystem = Awaited<ReturnType<typeof __unstable__loadDesignSystem>>
let ds: DesignSystem
const sites = new Map<string, Set<string>>()

beforeAll(async () => {
  ds = await __unstable__loadDesignSystem(readFileSync(join(SRC, 'index.css'), 'utf8'), { base: SRC })
  for (const file of sourceFiles(SRC)) {
    for (const w of words(readFileSync(file, 'utf8'))) {
      if (!CANDIDATE.test(w) || NOT_CLASSES.has(w)) continue
      if (!sites.has(w)) sites.set(w, new Set())
      sites.get(w)!.add(file.slice(SRC.length + 1))
    }
  }
}, 60_000)

describe('colour utilities', () => {
  // A floor that proves the scan reads the sources. The semantic names
  // collapse many palette classes into few, so the count stays low by design.
  it('finds the candidates', () => {
    expect(sites.size).toBeGreaterThan(200)
  })

  it('compiles every colour-like class in src/', () => {
    const candidates = [...sites.keys()]
    const css = ds.candidatesToCss(candidates)
    const unknown = candidates
      .filter((_, i) => !css[i])
      .map((c) => `${c} (${[...sites.get(c)!].slice(0, 3).join(', ')})`)
    expect(unknown).toEqual([])
  })

  it('defines the semantic families and the alias names', () => {
    const must = [
      'bg-surface-card', 'bg-surface-overlay', 'text-primary', 'text-tertiary', 'text-link',
      'border-subtle', 'border-strong', 'divide-subtle', 'bg-action', 'hover:bg-action-hover',
      'text-action-fg', 'bg-status-warning-bg', 'text-status-danger-fg', 'bg-accent-agent-solid',
      'bg-glass-fill', 'ring-focus-ring', 'shadow-elevation-3', 'bg-p-bg', 'text-p-text-light',
      'border-p-border-light', 'bg-brand', 'bg-brand-surface', 'hover:bg-p-bg-hover', 'text-p-primary',
    ]
    const css = ds.candidatesToCss(must)
    expect(must.filter((_, i) => !css[i])).toEqual([])
  })
})

describe('scales', () => {
  it('uses Tailwind\'s radius scale', () => {
    expect(ds.resolveThemeValue('--radius-lg')).toBe('0.5rem')
    expect(ds.resolveThemeValue('--radius-md')).toBe('0.375rem')
    expect(ds.resolveThemeValue('--radius-xl')).toBe('0.75rem')
    expect(ds.candidatesToCss(['rounded-lg'])[0]).toContain('border-radius: 0.5rem')
  })

  it('has no bare `rounded` left in src/', () => {
    const bare: string[] = []
    for (const file of sourceFiles(SRC)) {
      if (words(readFileSync(file, 'utf8')).includes('rounded')) bare.push(file.slice(SRC.length + 1))
    }
    expect(bare).toEqual([])
  })

  it('names the two small text sizes and the three faces', () => {
    const [xs2, xs3] = ds.candidatesToCss(['text-2xs', 'text-3xs'])
    expect(xs2).toContain('0.6875rem')
    expect(xs3).toContain('0.625rem')
    expect(ds.resolveThemeValue('--font-sans')).toMatch(/^Manrope,/)
    expect(ds.resolveThemeValue('--font-display')).toMatch(/^Comfortaa,/)
    expect(ds.resolveThemeValue('--font-mono')).toMatch(/^"JetBrains Mono",/)
  })

  it('names the durations and the control heights', () => {
    const css = ds.candidatesToCss(['duration-fast', 'duration-slow', 'ease-standard', 'h-control-md', 'rounded-control'])
    expect(css.every(Boolean)).toBe(true)
  })
})
