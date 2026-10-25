// @vitest-environment node
// The token generator (design/build.mjs): its checked-in outputs are
// current, its references resolve, and every contrast pair it promises
// holds in both modes.
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  DASHBOARD,
  KIT_EXCLUDED,
  buildOutputs,
  contrast,
  composite,
  cssColor,
  kitVariables,
  loadTokens,
  makeResolver,
  flatten,
  measureContrast,
  model,
  parseHex,
  referenceCss,
  themeKey,
  varName,
} from '../../design/build.mjs'

describe('generated outputs', () => {
  const outputs = buildOutputs()

  it.each([...outputs.keys()])('%s is current', (rel) => {
    const path = join(DASHBOARD, rel)
    // The public cut's gate runs this suite in the snapshot, where an
    // output's folder may be absent.
    if (!existsSync(dirname(path))) return
    expect(
      readFileSync(path, 'utf8') === outputs.get(rel),
      `${rel} is stale: run \`npm run tokens:build\` in dashboard/ and commit the result`,
    ).toBe(true)
  })

  it('design/reference/components.css is current', async () => {
    const path = join(DASHBOARD, 'design', 'reference', 'components.css')
    if (!existsSync(dirname(path))) return
    const css = await referenceCss(outputs.get('design/reference/index.html')!, DASHBOARD)
    expect(
      readFileSync(path, 'utf8') === css,
      'design/reference/components.css is stale: run `npm run tokens:build` in dashboard/ and commit the result',
    ).toBe(true)
  }, 60_000)
})

describe('the artifact kit', () => {
  // The names apps built before 1.9.0 read: every one stays, with its meaning.
  const PUBLIC_NAMES = [
    'p-bg', 'p-card', 'p-text', 'p-text-secondary', 'p-text-light', 'p-border', 'p-border-light',
    'p-surface', 'p-surface-hover', 'p-primary', 'p-primary-fg', 'p-brand-surface', 'p-accent-yellow',
    'p-accent-purple', 'p-accent-red', 'p-accent-teal', 'p-success', 'p-error', 'p-radius',
    'p-radius-sm', 'p-shadow', 'p-font', 'p-mono',
  ]
  const kit = buildOutputs().get('ui-kit/otodock-tokens.css')!
  const block = (selector: string) => kit.slice(kit.indexOf(`${selector} {`), kit.indexOf('}', kit.indexOf(`${selector} {`)))
  const names = (text: string) => new Set([...text.matchAll(/(--p-[a-z0-9-]+):/g)].map((m) => m[1].slice(2)))

  it('keeps every public name in both themes', () => {
    for (const selector of [':root', '.dark']) {
      const have = names(block(selector))
      expect(PUBLIC_NAMES.filter((n) => !have.has(n)), selector).toEqual([])
      expect(block(selector)).toContain('color-scheme:')
    }
  })

  it('keeps --p-primary readable as text and gives fills their own name', () => {
    const m = model()
    for (const mode of ['light', 'dark'] as const) {
      const value = (name: string) => (mode === 'light' ? block(':root') : block('.dark'))
        .match(new RegExp(`--${name}: (#[0-9a-f]{6})`))![1]
      expect(contrast(value('p-primary'), m.resolve('surface.page', mode) as string)).toBeGreaterThanOrEqual(4.5)
      expect(contrast(value('p-action-fg'), value('p-action'))).toBeGreaterThanOrEqual(4.5)
    }
  })
})

describe('the Figma variables', () => {
  type Value = { type?: string; name?: string }
  type Variable = { name: string; values: Record<string, Value> }
  const doc = JSON.parse(buildOutputs().get('design/figma-variables.json')!) as {
    collections: { name: string; modes: string[]; variables: Variable[] }[]
  }

  it('has the three collections with their modes', () => {
    expect(doc.collections.map((c) => `${c.name}:${c.modes.join('+')}`)).toEqual([
      'Primitives:Value', 'Semantic:Light+Dark', 'Component:Value',
    ])
  })

  it('aliases only variables that exist', () => {
    const names = new Set(doc.collections.flatMap((c) => c.variables.map((v) => v.name)))
    const dangling = doc.collections.flatMap((c) => c.variables.flatMap((v) => Object.values(v.values)
      .filter((x) => x.type === 'VARIABLE_ALIAS' && !names.has(x.name!))
      .map((x) => `${v.name} -> ${x.name}`)))
    expect(dangling).toEqual([])
  })
})

describe('the resolver', () => {
  it('follows references in the mode being built', () => {
    const m = model()
    expect(m.resolve('surface.page', 'light')).toBe('#faf9f9')
    expect(m.resolve('surface.page', 'dark')).toBe('#0d1117')
    expect(m.resolve('brand.default', 'dark')).toBe(m.resolve('action.default', 'dark'))
  })

  it('refuses an unknown reference', () => {
    const tree = { a: { $type: 'color', b: { $value: '{a.missing}' } } }
    expect(() => makeResolver(flatten(tree))('a.b', 'light')).toThrow(/unknown token \{a\.missing\}/)
  })

  it('refuses a cycle', () => {
    const tree = { a: { $type: 'color', b: { $value: '{a.c}' }, c: { $value: '{a.b}' } } }
    expect(() => makeResolver(flatten(tree))('a.b', 'light')).toThrow(/cycle/)
  })

  it('points every alias at a token', () => {
    const m = model()
    const tokens = flatten(loadTokens())
    for (const a of m.aliases) expect(tokens.has(a.target), a.name).toBe(true)
  })
})

describe('names', () => {
  it('keeps the bare name for action and brand only', () => {
    expect(varName(['action', 'default'])).toBe('--ot-action')
    expect(varName(['border', 'default'])).toBe('--ot-border-default')
    expect(themeKey(['text', 'primary'])).toBe('--text-color-primary')
    expect(themeKey(['border', 'strong'])).toBe('--border-color-strong')
    expect(themeKey(['status', 'warning', 'fg'])).toBe('--color-status-warning-fg')
  })

  it('writes translucent colours as rgb with alpha', () => {
    expect(cssColor('#ffffff0f')).toBe('rgb(255 255 255 / 0.06)')
    expect(cssColor('#146BB5')).toBe('#146bb5')
    expect(composite('#ffffff80', '#000000')).toBe('#808080')
  })
})

describe('contrast', () => {
  it('measures WCAG ratios', () => {
    expect(contrast('#ffffff', '#000000')).toBeCloseTo(21, 5)
    expect(contrast('#ffffff', '#146bb5')).toBeCloseTo(5.53, 2)
  })

  it('holds every promised pair in both modes', () => {
    const failing = measureContrast(model())
      .filter((r) => !r.pass)
      .map((r) => `${r.mode}: ${r.fg} on ${r.bg} is ${r.ratio.toFixed(2)}, needs ${r.min}`)
    expect(failing).toEqual([])
  })

  it('keeps the dark surfaces at least four lightness points apart', () => {
    const m = model()
    const lightness = (hex: string) => {
      const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
      return ((Math.max(r, g, b) + Math.min(r, g, b)) / 2) * 100
    }
    const steps = ['surface.page', 'surface.card', 'surface.raised', 'surface.overlay']
      .map((s) => lightness(m.resolve(s, 'dark') as string))
    for (let i = 1; i < steps.length; i++) expect(steps[i] - steps[i - 1]).toBeGreaterThanOrEqual(3.9)
  })
})

describe("the artifact kit's layer, names and pairs", () => {
  const kit = buildOutputs().get('ui-kit/otodock-tokens.css')!
  const block = (selector: string) => {
    const start = kit.indexOf(`\n${selector} {`)
    return kit.slice(start, kit.indexOf('\n}', start))
  }
  const values = (selector: string) => new Map([...block(selector).matchAll(/(--p-[a-z0-9-]+): ([^;]+);/g)].map((m) => [m[1], m[2]]))
  // A kit value as the generator's hex: `rgb(r g b / a)` is a translucent one.
  const hex = (css: string) => {
    if (css.startsWith('#')) return css
    const m = css.match(/^rgb\((\d+) (\d+) (\d+) \/ ([\d.]+)\)$/)!
    const h = (n: number) => Math.round(n).toString(16).padStart(2, '0')
    return `#${h(+m[1])}${h(+m[2])}${h(+m[3])}${h(+m[4] * 255)}`
  }
  const themes = [[':root', 'light'], ['.dark', 'dark']] as const
  const SIZES = ['3xs', '2xs', 'xs', 'sm', 'base', 'lg', 'xl', '2xl', '3xl']
  const NEW_NAMES = [
    'p-link-hover', 'p-action-active', 'p-action-subtle', 'p-action-subtle-hover', 'p-hover', 'p-scrim',
    'p-surface-inverse', 'p-text-inverse', 'p-danger-fill', 'p-danger-fill-hover', 'p-accent-agent-fill',
    'p-code-bg', 'p-code-fg', 'p-code-border', 'p-scrollbar-thumb', 'p-scrollbar-thumb-hover', 'p-control-track',
    'p-radius-popover', 'p-radius-chip', 'p-space', 'p-sidebar-width',
    ...SIZES.flatMap((s) => [`p-font-size-${s}`, `p-line-height-${s}`]),
    'p-control-sm', 'p-control-md', 'p-control-lg', 'p-duration-fast', 'p-duration-base', 'p-duration-slow',
    'p-ease-standard', 'p-ease-enter', 'p-ease-exit',
  ]

  it('opens with the layer statement and keeps its base inside the otodock layer', () => {
    const statement = kit.indexOf('@layer theme, base, otodock, components, utilities;')
    expect(statement).toBeGreaterThan(-1)
    expect(statement).toBeLessThan(kit.indexOf('@font-face'))
    const open = kit.indexOf('@layer otodock {')
    expect(open).toBeGreaterThan(kit.indexOf('\n.dark {'))
    expect(kit.indexOf('* { box-sizing: border-box; }')).toBeGreaterThan(open)
    expect(kit.trimEnd().endsWith('}')).toBe(true)
  })

  it('reads every token or leaves it out with a reason', () => {
    const m = model()
    const reads = new Set<string>()
    kitVariables(m, 'light', reads)
    const names = [...m.tokens.keys()]
    const excludedBy = (name: string) => KIT_EXCLUDED.find(([p]) => (p.endsWith('.') ? name.startsWith(p) : name === p))?.[0]
    expect(names.filter((n) => !excludedBy(n) && !reads.has(n))).toEqual([])
    for (const [p] of KIT_EXCLUDED) {
      expect(names.some((n) => excludedBy(n) === p), `${p} names a token`).toBe(true)
      expect([...reads].some((n) => excludedBy(n) === p), `${p} is not read`).toBe(false)
    }
  })

  it('exports the new names in both themes, the scales as the tokens give them', () => {
    for (const [selector] of themes) {
      const have = values(selector)
      expect(NEW_NAMES.filter((n) => !have.has(`--${n}`)), selector).toEqual([])
    }
    const light = values(':root')
    expect(light.get('--p-font-size-sm')).toBe('14px')
    expect(light.get('--p-line-height-sm')).toBe('20px')
    expect(light.get('--p-control-md')).toBe('36px')
    expect(light.get('--p-duration-fast')).toBe('120ms')
    expect(light.get('--p-ease-standard')).toBe('cubic-bezier(0.2, 0, 0, 1)')
    expect(light.get('--p-radius-chip')).toBe('9999px')
    expect(light.get('--p-space')).toBe('4px')
    expect(light.get('--p-sidebar-width')).toBe('208px')
  })

  it("holds the kit's own pairs on every surface it exports", () => {
    const failing: string[] = []
    for (const [selector, mode] of themes) {
      const vals = values(selector)
      const check = (fg: string, bg: string, min: number, over = '--p-bg') => {
        let b = hex(vals.get(bg)!)
        if (parseHex(b).a < 1) b = composite(b, hex(vals.get(over)!))
        const ratio = contrast(hex(vals.get(fg)!), b)
        if (ratio < min) failing.push(`${mode}: ${fg} on ${bg} is ${ratio.toFixed(2)}, needs ${min}`)
      }
      const surfaces = ['--p-bg', '--p-card', '--p-surface-raised', '--p-surface-overlay', '--p-surface-sunken', '--p-surface']
      for (const fg of ['--p-text', '--p-text-secondary', '--p-text-light', '--p-link', '--p-link-hover', '--p-primary']) {
        for (const bg of surfaces) check(fg, bg, 4.5)
        check(fg, '--p-hover', 4.5, '--p-card')
      }
      for (const t of ['status-success', 'status-warning', 'status-danger', 'status-info', 'accent-agent', 'accent-media']) {
        for (const bg of [`--p-${t}-bg`, '--p-card', '--p-surface-overlay', '--p-surface']) check(`--p-${t}-fg`, bg, 4.5)
        check('--p-text', `--p-${t}-bg`, 4.5)
        for (const bg of ['--p-bg', '--p-card', '--p-surface-raised', '--p-surface-overlay']) check(`--p-${t}-solid`, bg, 3)
      }
      check('--p-text-inverse', '--p-surface-inverse', 4.5)
      check('--p-code-fg', '--p-code-bg', 4.5)
      for (const bg of ['--p-danger-fill', '--p-danger-fill-hover', '--p-accent-agent-fill']) check('--p-on-solid', bg, 4.5)
      for (const bg of ['--p-action', '--p-action-hover', '--p-action-active']) check('--p-action-fg', bg, 4.5)
      check('--p-link', '--p-action-subtle', 4.5)
      check('--p-text', '--p-action-subtle', 4.5)
      check('--p-text', '--p-action-subtle-hover', 4.5)
      for (const bg of surfaces.slice(0, 5)) {
        check('--p-focus', bg, 3)
        check('--p-control-track', bg, 3)
      }
    }
    expect(failing).toEqual([])
  })

  it('keeps the field edge at 3:1 over every surface a field sits on', () => {
    // The edge is translucent, and a field clips its fill to the padding
    // box, so the edge sits on the surface behind the field.
    expect(values('.dark').get('--p-border-strong')).toMatch(/^rgb\(/)
    for (const [selector, mode] of themes) {
      const vals = values(selector)
      for (const bg of ['--p-bg', '--p-card', '--p-surface-raised', '--p-surface-overlay', '--p-surface-sunken']) {
        const surface = hex(vals.get(bg)!)
        const edge = composite(hex(vals.get('--p-border-strong')!), surface)
        expect(contrast(edge, surface), `${mode}: the edge on ${bg}`).toBeGreaterThanOrEqual(3)
      }
    }
  })

  it('keeps the browser root and puts the primitives on the scale', () => {
    const base = kit.slice(kit.indexOf('@layer otodock {'))
    expect(base).not.toContain('font-size: 14px')
    expect(base).toContain('body { padding: 2px; font-size: var(--p-font-size-sm); line-height: 1.4286; }')
    // The browser's [hidden] rule would lose to the kit's display rules.
    expect(base).toContain('[hidden]:where(:not([hidden="until-found"])) { display: none !important; }')
    // A heading sized under 18 px or with an arbitrary size keeps the
    // reading face; a colour utility (`text-[var(--p-text)]`) does not.
    expect(base).toContain(
      ':is(h1, h2):not([class*="text-3xs"]):not([class*="text-2xs"]):not([class*="text-xs"]):not([class*="text-sm"])' +
      ':not([class*="text-base"])' + [...'0123456789', '.', 'length:'].map((c) => `:not([class*="text-[${c}"])`).join('') +
      ':not([style*="font-size"]):not([style*="font:"]) { font-family: var(--p-font-display); }',
    )
    const rule = (selector: string) => {
      const start = base.indexOf(`\n${selector} {`)
      expect(start, selector).toBeGreaterThan(-1)
      return base.slice(start, base.indexOf('}', start))
    }
    const field = base.slice(base.indexOf(':where(input:not('), base.indexOf('\n', base.indexOf(':where(input:not(')))
    expect(field).toContain('background-clip: padding-box')
    expect(field).toContain('border: 1px solid var(--p-border-strong)')
    expect(field).toContain('min-height: var(--p-control-sm)')
    expect(rule('.card')).not.toContain('border:')
    expect(rule('.card .card')).toContain('border: 1px solid var(--p-border-light)')
    // An item inside a card sits on the page's colour, as the dashboard's.
    expect(rule('.card .card')).toContain('background: var(--p-bg)')
    // The button is the dashboard's secondary: a soft fill with primary
    // text, its hover the same fill a step further from the page.
    expect(rule('.btn')).toContain('background: var(--p-surface);')
    expect(rule('.btn')).toContain('color: var(--p-text);')
    expect(rule('.btn:hover:not(:disabled)')).toContain('background: var(--p-surface-hover)')
    expect(rule('.btn[aria-pressed="true"], .btn[aria-pressed="true"]:hover')).toContain('background: var(--p-action)')
    // A forced-colour mode drops the action fill: the chosen one takes the
    // system's selection colours instead.
    expect(base).toContain(
      '@media (forced-colors: active) { .btn[aria-pressed="true"] { forced-color-adjust: none; background: Highlight; color: HighlightText; border-color: Highlight; } }',
    )
    expect(rule('.btn.ghost')).toContain('background: transparent')
    expect(rule('.btn.ghost:hover')).toContain('background: var(--p-hover)')
    expect(rule('.btn.quiet-danger')).toContain('background: transparent')
    expect(rule('.badge')).toContain('background: var(--p-surface)')
    expect(rule('.stat .label')).toContain('margin: 0;')
    expect(rule('.stat .label')).toContain('font-weight: 400;')
    expect(rule('.btn')).toContain('min-height: var(--p-control-md)')
    expect(rule('pre')).toContain('background: var(--p-code-bg)')
    expect(rule('pre')).toContain('border: 1px solid var(--p-code-border)')
    expect(rule('tbody tr:hover')).toContain('var(--p-hover)')
    expect(rule('table')).toContain('font-size: var(--p-font-size-sm)')
  })

  it('pins the action pair at the ratio the app check measures', () => {
    for (const [selector] of themes) {
      const vals = values(selector)
      expect(contrast(vals.get('--p-action-fg')!, vals.get('--p-action')!)).toBeCloseTo(5.53, 2)
    }
  })

  it('the button reads at AA at rest and under the pointer', () => {
    for (const [selector] of themes) {
      const vals = values(selector)
      expect(contrast(vals.get('--p-text')!, vals.get('--p-surface')!)).toBeGreaterThanOrEqual(4.5)
      expect(contrast(vals.get('--p-text')!, vals.get('--p-surface-hover')!)).toBeGreaterThanOrEqual(4.5)
    }
  })
})
