// @vitest-environment node
// Controls a row shows on hover collapse only where the device can hover
// (classes.ts: ROW_TRAIL_HOVER_REVEAL, HOVER_REVEAL_FADE, HOVER_REVEAL_FLEX):
// a tablet or an open foldable is wider than `md` and has no hover. Tailwind
// 4 drops a class it cannot compile without a word, so each shape is compiled
// here with the dashboard's own design system, and the source is held to it.
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { __unstable__loadDesignSystem } from '@tailwindcss/node'
import { beforeAll, describe, expect, it } from 'vitest'

import { DASHBOARD } from '../../design/build.mjs'
import { HOVER_REVEAL_FADE, HOVER_REVEAL_FLEX, ROW_TRAIL_HOVER_REVEAL } from '../components/ui/classes'

const SRC = join(DASHBOARD, 'src')
const HOVER_MEDIA = /@media \(hover: ?hover\)/
const GATE = '[@media(hover:hover)]:'

type DesignSystem = Awaited<ReturnType<typeof __unstable__loadDesignSystem>>
let ds: DesignSystem
beforeAll(async () => {
  ds = await __unstable__loadDesignSystem(readFileSync(join(SRC, 'index.css'), 'utf8'), { base: SRC })
})

describe('the hover reveal shapes', () => {
  it.each([
    ['ROW_TRAIL_HOVER_REVEAL', ROW_TRAIL_HOVER_REVEAL],
    ['HOVER_REVEAL_FADE', HOVER_REVEAL_FADE],
    ['HOVER_REVEAL_FLEX', HOVER_REVEAL_FLEX],
  ])('%s collapses inside a hover media query only, and every class compiles', (_name, shape) => {
    const classes = shape.split(' ')
    const css = ds.candidatesToCss(classes)
    classes.forEach((cls, i) => expect(css[i], cls).toBeTruthy())
    const collapse = classes.filter((c) => c.startsWith(GATE))
    expect(collapse.length).toBeGreaterThan(0)
    for (const cls of collapse) {
      const rule = css[classes.indexOf(cls)] as string
      expect(rule, cls).toMatch(HOVER_MEDIA)
      expect(rule, cls).not.toMatch(/min-width/)
    }
    // Every reveal is the group's hover or its focus, never a width.
    for (const cls of classes.filter((c) => !c.startsWith(GATE))) {
      expect(cls).toMatch(/^group-(hover|focus-within):/)
    }
  })
})

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name)
    if (e.isDirectory()) {
      if (e.name !== 'tests') sourceFiles(p, out)
    } else if (/\.(ts|tsx)$/.test(e.name) && !e.name.endsWith('.d.ts')) {
      out.push(p)
    }
  }
  return out
}

// The composer's attachment chips decide by the pointer in code (`coarse`).
const KEPT = new Set(['components/chat/ChatInput.tsx'])

describe('no control is shown on hover alone by a width breakpoint', () => {
  it('the source has no width-gated or ungated hover reveal', () => {
    const offenders: string[] = []
    for (const file of sourceFiles(SRC)) {
      const rel = relative(SRC, file)
      if (KEPT.has(rel)) continue
      readFileSync(file, 'utf8').split('\n').forEach((line, i) => {
        if (/\b(sm|md|lg|xl):group-hover:/.test(line) || /(^|[\s'"`])opacity-0 [^'"`]*group-hover:opacity-100/.test(line)) {
          offenders.push(`${rel}:${i + 1}`)
        }
      })
    }
    expect(offenders).toEqual([])
  })
})
