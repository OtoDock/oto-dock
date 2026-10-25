// @vitest-environment node
// The brand export (design/brand/export.mjs): every public output is
// current from the master, the masters keep their shape, and the brand
// folder names nothing private.
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  BRAND,
  LOCKUP_STATES,
  MARK_BLUE,
  MARK_STATES,
  PUBLIC_OUTPUTS,
  REPO,
  compare,
  decodeIco,
  inlineMark,
  markSvg,
  pathData,
  readMaster,
  render,
  stateGroups,
} from '../../design/brand/export.mjs'

describe('the public outputs', () => {
  it.each(PUBLIC_OUTPUTS.map((row) => row.path))('%s is current', (path) => {
    const file = join(REPO, path)
    // The public cut's gate runs this suite in the snapshot, where an
    // output's folder may be absent.
    if (!existsSync(dirname(file))) return
    const row = PUBLIC_OUTPUTS.find((r) => r.path === path)!
    expect(
      compare(row, file),
      `${path} is stale: run \`npm run brand:export\` in dashboard/ and commit the result`,
    ).toBeNull()
  }, 30_000)

  it('the two favicons are the same bytes', () => {
    const [a, b] = ['dashboard/public/favicon.png', 'dashboard/public/assets/favicon.png'].map((p) => join(REPO, p))
    if (!existsSync(a) || !existsSync(b)) return
    expect(readFileSync(a).equals(readFileSync(b))).toBe(true)
  })

  it('the tray icon carries the four sizes Windows asks for', () => {
    const file = join(REPO, 'satellite', 'bin', 'otodock.ico')
    if (!existsSync(file)) return
    expect(decodeIco(readFileSync(file)).map((f) => f.width)).toEqual([16, 32, 48, 256])
  })
})

describe('the masters', () => {
  it('show one state, the flat one, so an <img> of the file shows the mark', () => {
    for (const [name, states] of [['otodock-mark.svg', MARK_STATES], ['otodock-lockup.svg', LOCKUP_STATES]] as const) {
      const groups = stateGroups(readMaster(name))
      expect(groups.map((g) => g.id)).toEqual(states)
      expect(groups.filter((g) => g.shown).map((g) => g.id)).toEqual(['flat'])
    }
  })

  it.each(MARK_STATES)('the %s state draws the mark inside the frame', (state) => {
    const { width, height, rgba } = render(markSvg(state))
    const alpha = (x: number, y: number) => rgba[(y * width + x) * 4 + 3]
    let covered = 0
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) if (alpha(x, y) > 127) covered++
    expect(covered / (width * height)).toBeGreaterThan(0.25)
    expect(covered / (width * height)).toBeLessThan(0.6)
    for (let x = 0; x < width; x++) {
      expect(alpha(x, 0)).toBe(0)
      expect(alpha(x, height - 1)).toBe(0)
    }
  })

  it('the mark is the tokens\' blue', () => {
    expect(MARK_BLUE).toBe('#1d4f80')
    for (const name of ['otodock-mark.svg', 'otodock-lockup.svg']) {
      expect(readMaster(name)).toMatch(new RegExp(`^<svg\\b[^>]* color="${MARK_BLUE}"`))
    }
    expect(readMaster()).toContain(`<stop offset="0" stop-color="${MARK_BLUE}"`)
  })

  it('the lockup draws the mark\'s own silhouette', () => {
    expect(pathData('silhouette', readMaster('otodock-lockup.svg'))).toBe(pathData('silhouette'))
  })

  it('an inline mark is one path that takes the CSS colour', () => {
    const svg = inlineMark({ size: 24, className: 'text-brand-mark' })
    expect(svg).toContain('fill="currentColor"')
    expect(svg).toContain(`d="${pathData('silhouette')}"`)
    expect(svg.match(/<path/g)).toHaveLength(1)
  })
})

describe('the brand folder ships in the public cut', () => {
  // Built from parts so this file does not carry the strings it looks for.
  const PRIVATE = [
    ['dashboard', 'docs', ''].join('/'),
    ['dashboard', 'android', ''].join('/'),
    ['otodock', 'commercial'].join('-'),
    ['design', 'system'].join('-'),
  ]

  it.each(readdirSync(BRAND))('%s names nothing private', (name) => {
    const text = readFileSync(join(BRAND, name), 'utf8')
    for (const word of PRIVATE) expect(text).not.toContain(word)
    expect(text).not.toMatch(/(^|[\s`'"(])dev\//m)
  })
})
