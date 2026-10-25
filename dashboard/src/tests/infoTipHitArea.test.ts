// @vitest-environment node
// The info button is a 32 px box. On a coarse pointer an invisible layer
// grows its hit area to 44 px, so a tap a few pixels beside the "i" in a
// checkbox row opens the info instead of toggling the setting. jsdom has no
// media queries, so this compiles the class string with Tailwind itself.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { __unstable__loadDesignSystem } from '@tailwindcss/node'
import { describe, expect, it } from 'vitest'

import { DASHBOARD } from '../../design/build.mjs'
import { INFO_TIP_BUTTON } from '../components/ui/classes'

const SRC = join(DASHBOARD, 'src')

describe('the info button hit area', () => {
  it('is 32 px on a fine pointer and 44 px on a coarse one', async () => {
    const ds = await __unstable__loadDesignSystem(readFileSync(join(SRC, 'index.css'), 'utf8'), { base: SRC })
    const classes = INFO_TIP_BUTTON.split(/\s+/)
    // The box: 8 spacing units of 4 px, the layer's frame.
    expect(classes).toContain('size-8')
    expect(classes).toContain('relative')
    const layer = classes.filter((c) => c.includes('before:'))
    expect(layer.every((c) => c.startsWith('pointer-coarse:'))).toBe(true)
    const css = ds.candidatesToCss(layer).join('\n')
    expect(css).toContain('@media (pointer: coarse)')
    expect(css).toContain('&::before')
    expect(css).toContain('position: absolute')
    // 1.5 spacing units past each edge: 32 + 2 × 6 = 44 px.
    expect(css).toContain('inset: calc(var(--spacing) * -1.5)')
  }, 60_000)
})
