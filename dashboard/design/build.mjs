// The token generator: reads design/tokens.json and writes every file the
// tokens feed. `npm run tokens:build` writes them; the test suite calls
// buildOutputs() in memory and fails when a checked-in output differs.
//
// Semantic tokens carry their light value in `$value` and their dark value
// in `$extensions["io.otodock.modes"].dark`; a reference is `{group.token}`
// and resolves in the mode being built. Public outputs only: nothing here
// names a private path, so the generator ships in the public cut as is.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { readClassStrings, referenceCss, referenceHtml } from './reference.mjs'

export { referenceCss }

export const DASHBOARD = join(dirname(fileURLToPath(import.meta.url)), '..')
export const TOKENS_PATH = join(DASHBOARD, 'design', 'tokens.json')
export const MODES = ['light', 'dark']
const MODES_EXT = 'io.otodock.modes'
const LINE_HEIGHT_EXT = 'io.otodock.lineHeight'
const TEXT_EXT = 'io.otodock.text'

export function loadTokens(path = TOKENS_PATH) {
  return JSON.parse(readFileSync(path, 'utf8'))
}

// --- Flatten and resolve ---------------------------------------------------

/** Every token as `{path, type, value, dark, description, extensions}`, with
 *  `$type` inherited from the nearest group. */
export function flatten(tree) {
  const out = new Map()
  const walk = (node, path, type) => {
    const t = node.$type ?? type
    if (Object.hasOwn(node, '$value')) {
      out.set(path.join('.'), {
        path,
        type: t,
        value: node.$value,
        dark: node.$extensions?.[MODES_EXT]?.dark,
        description: node.$description,
        extensions: node.$extensions ?? {},
      })
      return
    }
    for (const [key, child] of Object.entries(node)) {
      if (key.startsWith('$')) continue
      if (child === null || typeof child !== 'object' || Array.isArray(child)) {
        throw new Error(`tokens.json: ${[...path, key].join('.')} is neither a group nor a token`)
      }
      walk(child, [...path, key], t)
    }
  }
  walk(tree, [], undefined)
  return out
}

const REF = /^\{([^{}]+)\}$/

export function isRef(value) {
  return typeof value === 'string' && REF.test(value)
}

export function refTarget(value) {
  return value.match(REF)[1]
}

/** A resolver bound to one token set: `resolve(path, mode)` follows
 *  references in the same mode and fails on an unknown name or a cycle. */
export function makeResolver(tokens) {
  const resolve = (name, mode, seen = []) => {
    const tok = tokens.get(name)
    if (!tok) throw new Error(`tokens.json: unknown token {${name}}${seen.length ? ` (from ${seen.at(-1)})` : ''}`)
    if (seen.includes(name)) throw new Error(`tokens.json: reference cycle ${[...seen, name].join(' -> ')}`)
    const raw = mode === 'dark' && tok.dark !== undefined ? tok.dark : tok.value
    if (isRef(raw)) return resolve(refTarget(raw), mode, [...seen, name])
    return raw
  }
  return resolve
}

/** The first reference a token's value names in a mode (the primitive or
 *  semantic token it points at), or null for a literal. */
export function directRef(tokens, name, mode) {
  const tok = tokens.get(name)
  const raw = mode === 'dark' && tok.dark !== undefined ? tok.dark : tok.value
  return isRef(raw) ? refTarget(raw) : null
}

// --- Colour arithmetic -----------------------------------------------------

export function parseHex(hex) {
  const h = hex.replace('#', '')
  if (!/^([0-9a-f]{6}|[0-9a-f]{8})$/i.test(h)) throw new Error(`not a #rrggbb[aa] colour: ${hex}`)
  const n = (i) => parseInt(h.slice(i, i + 2), 16)
  return { r: n(0), g: n(2), b: n(4), a: h.length === 8 ? n(6) / 255 : 1 }
}

/** A colour as CSS: `#rrggbb`, or `rgb(r g b / a)` when translucent. */
export function cssColor(hex) {
  const { r, g, b, a } = parseHex(hex)
  if (a === 1) return hex.toLowerCase()
  return `rgb(${r} ${g} ${b} / ${Math.round(a * 100) / 100})`
}

/** `fg` laid over an opaque `bg`, as an opaque `#rrggbb`. */
export function composite(fg, bg) {
  const f = parseHex(fg)
  const b = parseHex(bg)
  const mix = (x, y) => Math.round(x * f.a + y * (1 - f.a))
  const hex = (v) => v.toString(16).padStart(2, '0')
  return `#${hex(mix(f.r, b.r))}${hex(mix(f.g, b.g))}${hex(mix(f.b, b.b))}`
}

export function luminance(hex) {
  const { r, g, b } = parseHex(hex)
  const lin = (v) => {
    const c = v / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

/** WCAG 2 contrast ratio; a translucent foreground is composited over the
 *  (opaque) background first. */
export function contrast(fg, bg) {
  const a = luminance(composite(fg, bg))
  const b = luminance(bg)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

// --- Names -----------------------------------------------------------------

// The groups whose `default` is the bare name: `action.default` is
// `bg-action`. Elsewhere `default` stays a word (`border-default`).
const BARE_DEFAULT = new Set(['action', 'brand'])

function named(path) {
  return path.at(-1) === 'default' && BARE_DEFAULT.has(path[0]) ? path.slice(0, -1) : path
}

/** The custom property a semantic token's value lives in (`--ot-…`, so it
 *  never meets one of Tailwind's own theme namespaces). */
export function varName(path) {
  return `--ot-${named(path).join('-')}`
}

const SEMANTIC_COLOUR_GROUPS = [
  'surface', 'text', 'border', 'action', 'brand', 'hero', 'status', 'accent',
  'glass', 'focus', 'code', 'scrollbar', 'control',
]

/** The Tailwind theme key a semantic colour maps to. Text colours use
 *  Tailwind's text-only key and borders its border-only key, so the
 *  utilities read `text-primary` and `border-subtle`. */
export function themeKey(path) {
  const [group, ...rest] = named(path)
  if (group === 'text') return `--text-color-${rest.join('-')}`
  if (group === 'border') return `--border-color-${rest.join('-')}`
  return `--color-${[group, ...rest].join('-')}`
}

function rem(px) {
  const n = parseFloat(px)
  if (!px.endsWith('px')) throw new Error(`expected px: ${px}`)
  return `${Math.round((n / 16) * 10000) / 10000}rem`
}

function fontStack(list) {
  return list.map((f) => (/[\s]/.test(f) && !f.startsWith('-') ? `"${f}"` : f)).join(', ')
}

function shadowCss(layers) {
  return layers
    .map((l) => `${l.inset ? 'inset ' : ''}${l.offsetX} ${l.offsetY} ${l.blur} ${l.spread} ${cssColor(l.color)}`)
    .join(', ')
}

function bezier(v) {
  return `cubic-bezier(${v.join(', ')})`
}

// --- The model the outputs read ---------------------------------------------

/** Everything the outputs need, resolved once: per-mode semantic colours,
 *  shadows, the alias table, the scales. */
export function model(tree = loadTokens()) {
  const tokens = flatten(tree)
  const resolve = makeResolver(tokens)
  const semantic = []
  const shadows = []
  const prose = []
  const aliases = []
  const textOverrides = []
  for (const [name, tok] of tokens) {
    const [group] = tok.path
    const textRef = tok.extensions[TEXT_EXT]
    if (textRef !== undefined) {
      // The same name reads a different token when it colours text: a
      // name that is a fill elsewhere (`brand`, an alias) keeps AA as text.
      const utility = group === 'alias' ? tok.path.slice(1).join('-') : themeKey(tok.path).replace(/^--color-/, '')
      textOverrides.push({ name: utility, target: refTarget(textRef) })
    }
    if (group === 'alias') {
      if (!isRef(tok.value)) throw new Error(`tokens.json: alias ${name} must reference a token`)
      aliases.push({ name: tok.path.slice(1).join('-'), target: refTarget(tok.value) })
    } else if (group === 'prose') {
      prose.push({ name: tok.path.slice(1).join('-'), target: refTarget(tok.value) })
    } else if (group === 'elevation') {
      shadows.push({
        path: tok.path,
        light: shadowCss(resolve(name, 'light')),
        dark: shadowCss(resolve(name, 'dark')),
        description: tok.description,
      })
    } else if (SEMANTIC_COLOUR_GROUPS.includes(group) && tok.type === 'color') {
      const light = resolve(name, 'light')
      const dark = resolve(name, 'dark')
      semantic.push({ path: tok.path, name, light, dark, description: tok.description })
    }
  }
  for (const m of MODES) for (const s of semantic) parseHex(s[m])
  const semanticByName = new Map(semantic.map((s) => [s.name, s]))
  for (const a of [...aliases, ...prose, ...textOverrides]) {
    if (!semanticByName.has(a.target) && !tokens.get(a.target)) {
      throw new Error(`tokens.json: {${a.target}} is not a token`)
    }
  }
  const dims = (prefix) =>
    [...tokens.values()].filter((t) => t.path.join('.').startsWith(prefix) && t.type === 'dimension')
  return {
    tree,
    tokens,
    resolve,
    semantic,
    semanticByName,
    shadows,
    prose,
    aliases,
    textOverrides,
    radius: dims('radius.').map((t) => ({ name: t.path[1], value: resolve(t.path.join('.'), 'light') })),
    componentRadius: [...tokens.values()]
      .filter((t) => t.path.at(-1) === 'radius' && t.path.length === 2)
      .map((t) => ({ name: t.path[0], value: resolve(t.path.join('.'), 'light') })),
    controlHeight: dims('control.height.').map((t) => ({ name: t.path[2], value: t.value })),
    fontFamily: [...tokens.values()].filter((t) => t.path[0] === 'font' && t.path[1] === 'family')
      .map((t) => ({ name: t.path[2], value: fontStack(t.value), description: t.description })),
    fontSize: [...tokens.values()].filter((t) => t.path[0] === 'font' && t.path[1] === 'size')
      .map((t) => ({ name: t.path[2], value: t.value, lineHeight: t.extensions[LINE_HEIGHT_EXT] })),
    duration: [...tokens.values()].filter((t) => t.path[0] === 'duration')
      .map((t) => ({ name: t.path[1], value: t.value, description: t.description })),
    easing: [...tokens.values()].filter((t) => t.path[0] === 'easing')
      .map((t) => ({ name: t.path[1], value: bezier(t.value), description: t.description })),
    plain: [...tokens.values()].filter((t) => ['topbar', 'sidebar'].includes(t.path[0]))
      .map((t) => ({ name: t.path.join('-'), value: t.value })),
  }
}

/** The value an alias or a prose variable points at, as CSS: the semantic
 *  token's custom property, or a primitive's literal value. */
function targetCss(m, target) {
  const s = m.semanticByName.get(target)
  if (s) return `var(${varName(s.path)})`
  const value = m.resolve(target, 'light')
  if (value !== m.resolve(target, 'dark')) throw new Error(`alias target {${target}} has modes but is not semantic`)
  return cssColor(value)
}

// --- Contrast ------------------------------------------------------------------

const SURFACES = [
  'surface.page', 'surface.card', 'surface.raised', 'surface.overlay',
  'surface.sunken', 'surface.muted', 'surface.hover', 'surface.chrome',
]

/** The pairs the tokens promise, in both modes: text at 4.5:1 (AA), the
 *  boundaries and indicators a person must see at 3:1. A translucent
 *  background is measured over `over` (the page by default). */
export function contrastPairs() {
  const pairs = []
  const add = (fg, bg, min, over) => pairs.push(over ? { fg, bg, min, over } : { fg, bg, min })
  for (const fg of ['text.primary', 'text.secondary', 'text.tertiary', 'text.link', 'text.link-hover']) {
    for (const bg of SURFACES) add(fg, bg, 4.5)
    add(fg, 'surface.hover', 4.5, 'surface.card')
  }
  for (const fg of ['text.primary', 'text.secondary']) {
    add(fg, 'surface.hover', 4.5, 'surface.overlay')
    add(fg, 'glass.fill', 4.5)
  }
  add('text.inverse', 'surface.inverse', 4.5)
  for (const [group, names] of [['status', ['success', 'warning', 'danger', 'info']], ['accent', ['agent', 'media']]]) {
    for (const n of names) {
      const t = `${group}.${n}`
      for (const bg of [`${t}.bg`, 'surface.card', 'surface.overlay', 'surface.muted']) add(`${t}.fg`, bg, 4.5)
      add('text.primary', `${t}.bg`, 4.5)
      add('text.secondary', `${t}.bg`, 4.5)
      for (const bg of ['surface.page', 'surface.card', 'surface.raised', 'surface.overlay']) add(`${t}.solid`, bg, 3)
    }
  }
  for (const bg of ['action.default', 'action.hover', 'action.active']) add('action.fg', bg, 4.5)
  for (const bg of ['status.danger.fill', 'status.danger.fill-hover', 'accent.agent.fill']) add('text.on-solid', bg, 4.5)
  add('text.link', 'action.subtle', 4.5)
  add('text.primary', 'action.subtle', 4.5)
  add('text.primary', 'action.subtle-hover', 4.5)
  for (const bg of ['surface.page', 'surface.card', 'surface.raised', 'surface.overlay', 'surface.sunken']) {
    add('border.strong', bg, 3)
    add('focus.ring', bg, 3)
    add('control.track', bg, 3)
  }
  for (const bg of ['surface.page', 'surface.card']) add('brand.mark', bg, 3)
  add('hero.text', 'hero.bg', 4.5)
  add('hero.text-muted', 'hero.bg', 4.5)
  add('code.fg', 'code.bg', 4.5)
  return pairs
}

/** Each pair measured in each mode. A translucent background is laid over
 *  its `over` surface first, a translucent foreground over its background. */
export function measureContrast(m) {
  const rows = []
  for (const pair of contrastPairs()) {
    for (const mode of MODES) {
      let bg = m.resolve(pair.bg, mode)
      if (parseHex(bg).a < 1) bg = composite(bg, m.resolve(pair.over ?? 'surface.page', mode))
      const ratio = contrast(m.resolve(pair.fg, mode), bg)
      rows.push({ ...pair, mode, ratio, pass: ratio >= pair.min })
    }
  }
  return rows
}

// --- Output: src/tokens.css --------------------------------------------------

const HEADER = (source) =>
  `/* Generated by \`npm run tokens:build\` from design/tokens.json: edit that
   file, never this one (a test fails when the two disagree). ${source} */\n`

export function tokensCss(m) {
  const lines = [HEADER('The dashboard\'s colour, type, radius, elevation and motion tokens.')]
  const block = (selector, mode, extra) => {
    lines.push(`${selector} {`)
    lines.push(...extra)
    for (const s of m.semantic) lines.push(`  ${varName(s.path)}: ${cssColor(s[mode])};`)
    for (const s of m.shadows) lines.push(`  ${varName(s.path)}: ${s[mode]};`)
    if (mode === 'light') {
      for (const p of m.plain) lines.push(`  --ot-${p.name}: ${p.value};`)
      for (const h of m.controlHeight) lines.push(`  --ot-control-height-${h.name}: ${h.value};`)
    }
    lines.push('}')
  }
  block(':root', 'light', [
    '  /* Must MATCH the artifact and app iframes\' documents (the kit\'s tokens set',
    '     color-scheme per theme): when an embedded document\'s used color-scheme',
    '     differs from its embedding element\'s, browsers paint an OPAQUE canvas',
    '     behind the deliberately transparent artifact body. */',
    '  color-scheme: light;',
  ])
  lines.push('')
  block('.dark', 'dark', ['  color-scheme: dark;'])
  lines.push('')
  lines.push('/* The utilities. `inline` makes each utility read the variable itself, so')
  lines.push('   a theme switch is live and opacity modifiers mix over it. */')
  lines.push('@theme inline {')
  for (const s of m.semantic) lines.push(`  ${themeKey(s.path)}: var(${varName(s.path)});`)
  lines.push('')
  lines.push('  /* The alias names, each on its semantic successor. */')
  for (const a of m.aliases) lines.push(`  --color-${a.name}: ${targetCss(m, a.target)};`)
  lines.push('')
  lines.push('  /* Those of them that are fills elsewhere, when they colour text. */')
  for (const o of m.textOverrides) lines.push(`  --text-color-${o.name}: ${targetCss(m, o.target)};`)
  lines.push('')
  for (const f of m.fontFamily) lines.push(`  --font-${f.name}: ${f.value};`)
  for (const f of m.fontSize) {
    lines.push(`  --text-${f.name}: ${rem(f.value)};`)
    lines.push(`  --text-${f.name}--line-height: ${rem(f.lineHeight)};`)
  }
  for (const r of m.radius) {
    if (r.name === 'none' || r.name === 'full') continue
    lines.push(`  --radius-${r.name}: ${rem(r.value)};`)
  }
  for (const r of m.componentRadius) {
    lines.push(`  --radius-${r.name}: ${r.value === '9999px' ? 'calc(infinity * 1px)' : rem(r.value)};`)
  }
  for (const h of m.controlHeight) lines.push(`  --spacing-control-${h.name}: ${rem(h.value)};`)
  for (const s of m.shadows) lines.push(`  --shadow-${s.path.join('-')}: var(${varName(s.path)});`)
  for (const d of m.duration) lines.push(`  --transition-duration-${d.name}: ${d.value};`)
  for (const e of m.easing) lines.push(`  --ease-${e.name}: ${e.value};`)
  lines.push('}')
  lines.push('')
  lines.push('/* The typography plugin reads these; prose and prose-invert get the same')
  lines.push('   tokens, which already follow the theme. */')
  lines.push('.prose {')
  for (const p of m.prose) {
    const v = targetCss(m, p.target)
    lines.push(`  --tw-prose-${p.name}: ${v};`)
    lines.push(`  --tw-prose-invert-${p.name}: ${v};`)
  }
  lines.push('}')
  return lines.join('\n') + '\n'
}

// --- Output: ui-kit/otodock-tokens.css (the artifact kit) ----------------------

const LATIN =
  'U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,' +
  'U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD'
const GREEK = 'U+0370-0377,U+037A-037F,U+0384-038A,U+038C,U+038E-03A1,U+03A3-03FF'
const KIT_FONTS = [
  ['Manrope', 'manrope', [400, 500, 600, 700]],
  ['Comfortaa', 'comfortaa', [400, 500, 600, 700]],
  ['JetBrains Mono', 'jetbrains-mono', [400, 700]],
]

/** The tokens the kit leaves out, each with its reason; a prefix ends with
 *  a dot. The coverage test holds every other token to a kit name. */
export const KIT_EXCLUDED = [
  ['color.', 'nothing paints with a primitive'],
  ['radius.', 'radii reach the kit by component: the 4 px step is written into the code and scrollbar rules, the 16 px step is the hero surfaces'],
  ['brand.', "the mark and its blue are the platform's alone"],
  ['hero.', 'the sign-in and boot surfaces'],
  ['glass.', 'the chat chrome'],
  ['prose.', "the typography plugin's variables"],
  ['alias.', "the dashboard's earlier Tailwind names"],
  ['surface.chrome', 'the chat chrome'],
  ['composer.radius', 'the chat composer'],
  ['topbar.height', 'the chat top bar'],
]

/** The kit's public names: every one an app may already read stays, with its
 *  meaning; new names are added beside them, none removed. `reads`, when
 *  given, collects every token path the kit resolves. */
export function kitVariables(m, mode, reads = null) {
  const read = (name) => { reads?.add(name); return name }
  const v = (name) => {
    const s = m.semanticByName.get(read(name))
    if (!s) throw new Error(`kit: {${name}} is not a semantic colour`)
    return s[mode]
  }
  const solid = (name, over) => {
    const c = v(name)
    return parseHex(c).a < 1 ? composite(c, v(over)) : c
  }
  const shadow = (n) => { read(`elevation.${n}`); return m.shadows.find((s) => s.path[1] === String(n))[mode] }
  const font = (n) => { read(`font.family.${n}`); return m.fontFamily.find((f) => f.name === n).value }
  const radius = (n) => { read(`${n}.radius`); return m.componentRadius.find((r) => r.name === n).value }
  const plain = (name) => m.resolve(read(name), mode)
  const out = [
    ['--p-bg', v('surface.page')],
    ['--p-card', v('surface.card')],
    ['--p-text', v('text.primary')],
    ['--p-text-secondary', v('text.secondary')],
    ['--p-text-light', v('text.tertiary')],
    ['--p-border', solid('border.default', 'surface.card')],
    ['--p-border-light', solid('border.subtle', 'surface.card')],
    ['--p-surface', v('surface.muted')],
    ['--p-surface-hover', solid('surface.hover', 'surface.muted')],
    // Apps read --p-primary as text and as a fill; it stays readable as text
    // in both themes. A fill under white text is --p-action.
    ['--p-primary', v('text.link')],
    ['--p-primary-fg', v('action.fg')],
    ['--p-brand-surface', v('action.subtle')],
    ['--p-accent-yellow', v('status.warning.solid')],
    ['--p-accent-purple', v('accent.agent.fill')],
    ['--p-accent-red', v('status.danger.fill')],
    ['--p-accent-teal', v('accent.media.solid')],
    ['--p-success', v('status.success.solid')],
    ['--p-error', v('status.danger.solid')],
    ['--p-radius', radius('card')],
    ['--p-radius-sm', radius('control')],
    ['--p-shadow', shadow(1)],
    ['--p-font', font('sans')],
    ['--p-mono', font('mono')],
    ['--p-link', v('text.link')],
    ['--p-action', v('action.default')],
    ['--p-action-hover', v('action.hover')],
    ['--p-action-fg', v('action.fg')],
    ['--p-on-solid', v('text.on-solid')],
    ['--p-surface-raised', v('surface.raised')],
    ['--p-surface-overlay', v('surface.overlay')],
    ['--p-surface-sunken', v('surface.sunken')],
    // Translucent, so the edge keeps 3:1 on every surface; a field clips its
    // own fill to the padding box, so the edge sits on the surface behind it.
    ['--p-border-strong', v('border.strong')],
    ['--p-focus', v('focus.ring')],
    ['--p-radius-lg', radius('dialog')],
    ['--p-font-display', font('display')],
    ['--p-link-hover', v('text.link-hover')],
    ['--p-action-active', v('action.active')],
    ['--p-action-subtle', v('action.subtle')],
    ['--p-action-subtle-hover', v('action.subtle-hover')],
    ['--p-hover', v('surface.hover')],
    ['--p-scrim', v('surface.scrim')],
    ['--p-surface-inverse', v('surface.inverse')],
    ['--p-text-inverse', v('text.inverse')],
    ['--p-danger-fill', v('status.danger.fill')],
    ['--p-danger-fill-hover', v('status.danger.fill-hover')],
    ['--p-accent-agent-fill', v('accent.agent.fill')],
    ['--p-code-bg', v('code.bg')],
    ['--p-code-fg', v('code.fg')],
    ['--p-code-border', v('code.border')],
    ['--p-scrollbar-thumb', v('scrollbar.thumb')],
    ['--p-scrollbar-thumb-hover', v('scrollbar.thumb-hover')],
    ['--p-control-track', v('control.track')],
    ['--p-radius-popover', radius('popover')],
    ['--p-radius-chip', radius('chip')],
  ]
  for (const s of ['success', 'warning', 'danger', 'info']) {
    for (const slot of ['fg', 'bg', 'border', 'solid']) out.push([`--p-status-${s}-${slot}`, v(`status.${s}.${slot}`)])
  }
  for (const a of ['agent', 'media']) {
    for (const slot of ['fg', 'bg', 'border', 'solid']) out.push([`--p-accent-${a}-${slot}`, v(`accent.${a}.${slot}`)])
  }
  for (const n of [1, 2, 3, 4]) out.push([`--p-elevation-${n}`, shadow(n)])
  for (const f of m.fontSize) {
    read(`font.size.${f.name}`)
    out.push([`--p-font-size-${f.name}`, f.value], [`--p-line-height-${f.name}`, f.lineHeight])
  }
  for (const h of m.controlHeight) { read(`control.height.${h.name}`); out.push([`--p-control-${h.name}`, h.value]) }
  for (const d of m.duration) { read(`duration.${d.name}`); out.push([`--p-duration-${d.name}`, d.value]) }
  for (const e of m.easing) { read(`easing.${e.name}`); out.push([`--p-ease-${e.name}`, e.value]) }
  out.push(['--p-space', plain('spacing.unit')], ['--p-sidebar-width', plain('sidebar.width')])
  return out.map(([k, val]) => [k, val.startsWith('#') ? cssColor(val) : val])
}

/* The layer statement is the kit's first rule: Tailwind's own statement
   (theme, base, components, utilities) comes later and cannot reorder it. */
const KIT_LAYERS = '@layer theme, base, otodock, components, utilities;'

const KIT_BASE = `/* --- Base and primitives ---
   Inside the otodock layer, between Tailwind's reset and its utilities: the
   kit beats the reset, a utility on a kit element beats the kit, and a page's
   own unlayered rules beat both. */
@layer otodock {

* { box-sizing: border-box; }
/* The kit's own display rules would otherwise beat the browser's [hidden]
   rule on a page without Tailwind's reset. */
[hidden]:where(:not([hidden="until-found"])) { display: none !important; }

html, body {
  margin: 0;
  padding: 0;
  background: transparent;            /* seamless in the transcript */
  color: var(--p-text);
  font-family: var(--p-font);
  -webkit-font-smoothing: antialiased;
  /* Kill the mobile double-tap-to-zoom heuristic inside artifact frames:
     it makes browsers hold or swallow single taps on interactive controls
     (buttons needing two presses on phones). Pan and pinch still work. */
  touch-action: manipulation;
}

/* The root keeps the browser's 16 px, so Tailwind's rem scale is the
   dashboard's; the body reads at the small size on a unitless line. */
body { padding: 2px; font-size: var(--p-font-size-sm); line-height: 1.4286; }

h1, h2, h3, h4 { color: var(--p-text); font-weight: 600; margin: 0 0 8px; }
/* The display face at the kit's own heading sizes only: a heading an author
   sizes under 18 px, or with an arbitrary size, keeps the reading face; a
   colour or alignment utility leaves the face alone. */
:is(h1, h2):not([class*="text-3xs"]):not([class*="text-2xs"]):not([class*="text-xs"]):not([class*="text-sm"]):not([class*="text-base"]):not([class*="text-[0"]):not([class*="text-[1"]):not([class*="text-[2"]):not([class*="text-[3"]):not([class*="text-[4"]):not([class*="text-[5"]):not([class*="text-[6"]):not([class*="text-[7"]):not([class*="text-[8"]):not([class*="text-[9"]):not([class*="text-[."]):not([class*="text-[length:"]):not([style*="font-size"]):not([style*="font:"]) { font-family: var(--p-font-display); }
h1 { font-size: var(--p-font-size-2xl); line-height: var(--p-line-height-2xl); }
h2 { font-size: var(--p-font-size-xl); line-height: var(--p-line-height-xl); }
h3 { font-size: var(--p-font-size-base); line-height: var(--p-line-height-base); }
h4 { font-size: var(--p-font-size-sm); line-height: var(--p-line-height-sm); }
p { margin: 0 0 12px; }
a { color: var(--p-link); text-decoration: none; }
a:hover { color: var(--p-link-hover); text-decoration: underline; }
small, .muted { color: var(--p-text-secondary); }
:focus-visible { outline: 2px solid var(--p-focus); outline-offset: 2px; }

/* Scrollbars on the kit's two names, as the dashboard draws its own. */
:root { scrollbar-color: var(--p-scrollbar-thumb) transparent; }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--p-scrollbar-thumb); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--p-scrollbar-thumb-hover); }

code, pre { font-family: var(--p-mono); font-size: var(--p-font-size-xs); border-radius: 4px; }
code { padding: 2px 6px; background: var(--p-surface); }
/* The code slab is dark in both themes; its edge is what shows where the
   slab sits two steps under a dark page. */
pre { padding: 12px 16px; overflow-x: auto; background: var(--p-code-bg); color: var(--p-code-fg); border: 1px solid var(--p-code-border); }
pre code { padding: 0; background: none; color: inherit; }

/* --- Primitives --- */

/* Card: its surface and elevation 1 set it apart. A card inside a card is an
   item: the page's own colour and an edge, a recess rather than a raise. */
.card { background: var(--p-card); border-radius: var(--p-radius); box-shadow: var(--p-elevation-1); padding: 16px; }
.card .card { background: var(--p-bg); box-shadow: none; border: 1px solid var(--p-border-light); }

/* Table */
table { border-collapse: collapse; width: 100%; font-size: var(--p-font-size-sm); }
thead th { text-align: left; font-size: var(--p-font-size-xs); font-weight: 600; color: var(--p-text-secondary); text-transform: uppercase; letter-spacing: .04em; border-bottom: 1px solid var(--p-border-light); padding: 12px 16px; white-space: nowrap; }
tbody td { padding: 12px 16px; border-bottom: 1px solid var(--p-border-light); }
tbody tr:hover { background: var(--p-hover); }
tbody tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }

/* Button: secondary by default (a soft fill on the neutral surface, its
   hover the same fill one step further from the page), then .primary,
   .danger, .ghost and .quiet-danger, with .sm and .lg for the other two
   control heights. A segmented control is a row of them with the chosen
   one aria-pressed, on the action fill. */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: var(--p-control-md); padding: 6px 16px; font: inherit; font-size: var(--p-font-size-sm); font-weight: 500; color: var(--p-text); background: var(--p-surface); border: 1px solid var(--p-border); border-radius: var(--p-radius-sm); cursor: pointer; transition: background-color var(--p-duration-fast) var(--p-ease-standard), color var(--p-duration-fast) var(--p-ease-standard); }
.btn:hover:not(:disabled) { background: var(--p-surface-hover); color: var(--p-text); }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn.primary { background: var(--p-action); color: var(--p-action-fg); border-color: transparent; }
.btn.primary:hover { background: var(--p-action-hover); color: var(--p-action-fg); }
.btn.primary:active { background: var(--p-action-active); }
.btn.danger { background: var(--p-danger-fill); color: var(--p-on-solid); border-color: transparent; }
.btn.danger:hover { background: var(--p-danger-fill-hover); color: var(--p-on-solid); }
.btn.ghost { background: transparent; border-color: transparent; color: var(--p-text-secondary); }
.btn.ghost:hover { background: var(--p-hover); color: var(--p-text); }
.btn.quiet-danger { min-height: 28px; padding: 2px 10px; font-size: var(--p-font-size-xs); background: transparent; border-color: transparent; color: var(--p-status-danger-fg); }
.btn.quiet-danger:hover { background: var(--p-status-danger-bg); color: var(--p-status-danger-fg); }
.btn[aria-pressed="true"], .btn[aria-pressed="true"]:hover { background: var(--p-action); color: var(--p-action-fg); border-color: var(--p-action); }
.btn.sm { min-height: var(--p-control-sm); padding: 4px 12px; }
.btn.lg { min-height: var(--p-control-lg); padding: 8px 20px; font-size: var(--p-font-size-base); }

/* Fields: the edge is translucent, so the fill stops at the padding box and
   the edge sits on the surface behind the field. :where keeps the focus and
   invalid rules below in charge. */
:where(input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="file"]):not([type="color"]):not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="image"]):not([type="hidden"]), select, textarea) { min-width: 0; min-height: var(--p-control-sm); padding: 6px 12px; font: inherit; font-size: var(--p-font-size-sm); color: var(--p-text); background: var(--p-card); background-clip: padding-box; border: 1px solid var(--p-border-strong); border-radius: var(--p-radius-sm); }
input::placeholder, textarea::placeholder { color: var(--p-text-light); }
input:focus, select:focus, textarea:focus { border-color: var(--p-action); }
[aria-invalid="true"] { border-color: var(--p-status-danger-solid); }
.label { display: block; margin-bottom: 4px; font-size: var(--p-font-size-xs); font-weight: 500; color: var(--p-text); }

/* Stat */
.stat { display: flex; flex-direction: column; gap: 2px; }
.stat .value { font-size: var(--p-font-size-2xl); line-height: var(--p-line-height-2xl); font-weight: 600; color: var(--p-text); font-variant-numeric: tabular-nums; }
.stat .label { margin: 0; font-size: var(--p-font-size-2xs); line-height: var(--p-line-height-2xs); font-weight: 400; color: var(--p-text-secondary); text-transform: uppercase; letter-spacing: .04em; }

/* Layout helpers */
.row { display: flex; gap: 16px; flex-wrap: wrap; }
.grid { display: grid; gap: 16px; }

/* Badge: a tint with its own foreground, grey unless a tone says otherwise */
.badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: var(--p-radius-chip); font-size: var(--p-font-size-xs); line-height: var(--p-line-height-xs); font-weight: 500; white-space: nowrap; background: var(--p-surface); color: var(--p-text-secondary); }
.badge.info { background: var(--p-status-info-bg); color: var(--p-status-info-fg); }
.badge.success { background: var(--p-status-success-bg); color: var(--p-status-success-fg); }
.badge.warn, .badge.warning { background: var(--p-status-warning-bg); color: var(--p-status-warning-fg); }
.badge.error, .badge.danger { background: var(--p-status-danger-bg); color: var(--p-status-danger-fg); }
.badge.agent { background: var(--p-accent-agent-bg); color: var(--p-accent-agent-fg); }
.badge.media { background: var(--p-accent-media-bg); color: var(--p-accent-media-fg); }
.badge.action { background: var(--p-action-subtle); color: var(--p-link); }

@media (prefers-reduced-motion: reduce) { .btn { transition: none; } }
/* A forced-colour mode drops the fills: the chosen segment takes the
   system's selection colours, or it is the same frame as the others. */
@media (forced-colors: active) { .btn[aria-pressed="true"] { forced-color-adjust: none; background: Highlight; color: HighlightText; border-color: Highlight; } }

}
`

export function kitCss(m) {
  const lines = [
    `/* The UI kit's tokens, fonts and primitives for artifacts and app pages:
   generated by \`npm run tokens:build\` from design/tokens.json, so an
   artifact reads the same values as the dashboard in both themes. The
   runtime sets \`.dark\` on <html>. Fonts are declared here because the
   sandboxed artifact frame never loads the dashboard's bundled webfonts.
   Every --p-* name an app may read stays; new names are added beside them. */`,
    '',
    KIT_LAYERS,
    '',
    '/* --- Fonts (woff2s copied to /ui-kit/fonts/ at build; latin + greek) --- */',
    '',
  ]
  for (const [family, file, weights] of KIT_FONTS) {
    for (const w of weights) {
      for (const [subset, range] of [['latin', LATIN], ['greek', GREEK]]) {
        lines.push('@font-face {')
        lines.push(`  font-family: '${family}'; font-style: normal; font-display: swap; font-weight: ${w};`)
        lines.push(`  src: url(/ui-kit/fonts/${file}-${subset}-${w}-normal.woff2) format('woff2');`)
        lines.push(`  unicode-range: ${range};`)
        lines.push('}')
      }
    }
  }
  lines.push('', '/* --- Tokens --- */', '')
  lines.push(':root {')
  for (const [k, val] of kitVariables(m, 'light')) lines.push(`  ${k}: ${val};`)
  lines.push(
    '  /* Keep color-scheme: it must match the dashboard page that holds the',
    '     frame. When the two differ, the browser paints an opaque canvas',
    '     behind the deliberately transparent artifact body. */',
  )
  lines.push('  color-scheme: light;')
  lines.push('}', '')
  lines.push('.dark {')
  for (const [k, val] of kitVariables(m, 'dark')) lines.push(`  ${k}: ${val};`)
  lines.push('  color-scheme: dark;')
  lines.push('}', '')
  return lines.join('\n') + '\n' + KIT_BASE
}

// --- Output: src/uikit/tokens.generated.ts (the widget kit's constants) -------

export function widgetConstants(m) {
  const v = (name) => cssColor(m.semanticByName.get(name).light)
  const entries = [
    ['font', m.fontFamily.find((f) => f.name === 'sans').value],
    ['text', v('text.primary')],
    ['textSecondary', v('text.secondary')],
    ['textTertiary', v('text.tertiary')],
    ['link', v('text.link')],
    ['surface', v('surface.muted')],
    ['card', v('surface.card')],
    ['border', v('border.subtle')],
    ['borderDefault', v('border.default')],
    ['action', v('action.default')],
    ['actionHover', v('action.hover')],
    ['actionFg', v('action.fg')],
    ['brandSurface', v('action.subtle')],
    ['focus', v('focus.ring')],
  ]
  for (const s of ['success', 'warning', 'danger', 'info']) {
    entries.push([`${s}Bg`, v(`status.${s}.bg`)], [`${s}Fg`, v(`status.${s}.fg`)])
  }
  const body = entries.map(([k, val]) => `  ${k}: '${val}',`).join('\n')
  return `// Generated by \`npm run tokens:build\` from design/tokens.json: edit that
// file, never this one. The widget kit's light values, the fallbacks behind
// the kit's --p-* variables when a page has not loaded the kit's tokens.
export const WIDGET_TOKENS = {
${body}
} as const
`
}

// --- Output: design/figma-variables.json ----------------------------------------

function figmaColour(hex) {
  const { r, g, b, a } = parseHex(hex)
  const f = (v) => Math.round((v / 255) * 10000) / 10000
  return { r: f(r), g: f(g), b: f(b), a: Math.round(a * 10000) / 10000 }
}

/** The variables Figma mirrors (the Figma file is written from them):
 *  Primitives in one mode, Semantic in Light and Dark aliasing the
 *  primitives by name, Component aliasing the scales, and the elevation
 *  levels as effect definitions (Figma keeps shadows as effect styles). */
export function figmaVariables(m) {
  const slash = (name) => name.replace(/\./g, '/')
  const primitives = []
  const semantic = []
  const component = []
  for (const [name, tok] of m.tokens) {
    const [group] = tok.path
    if (group === 'color') {
      primitives.push({ name: slash(name), type: 'COLOR', values: { Value: figmaColour(tok.value) } })
    } else if (group === 'radius' || (group === 'font' && tok.path[1] === 'size')) {
      primitives.push({ name: slash(name), type: 'FLOAT', values: { Value: parseFloat(tok.value) } })
    } else if (group === 'font' && tok.path[1] === 'family') {
      primitives.push({ name: slash(name), type: 'STRING', values: { Value: tok.value[0] } })
    }
  }
  const valueIn = (name, mode) => {
    const ref = directRef(m.tokens, name, mode)
    if (ref) return { type: 'VARIABLE_ALIAS', name: slash(ref) }
    return figmaColour(m.resolve(name, mode))
  }
  for (const s of m.semantic) {
    semantic.push({
      name: slash(s.name),
      type: 'COLOR',
      values: { Light: valueIn(s.name, 'light'), Dark: valueIn(s.name, 'dark') },
      ...(s.description ? { description: s.description } : {}),
    })
  }
  for (const [name, tok] of m.tokens) {
    if (tok.type !== 'dimension' || ['radius', 'font', 'spacing'].includes(tok.path[0])) continue
    const ref = isRef(tok.value) ? refTarget(tok.value) : null
    component.push({
      name: slash(name),
      type: 'FLOAT',
      values: { Value: ref ? { type: 'VARIABLE_ALIAS', name: slash(ref) } : parseFloat(tok.value) },
    })
  }
  const effects = m.shadows.map((s) => ({
    name: slash(s.path.join('.')),
    modes: Object.fromEntries(MODES.map((mode) => [mode === 'light' ? 'Light' : 'Dark', m.resolve(s.path.join('.'), mode)])),
    ...(s.description ? { description: s.description } : {}),
  }))
  const doc = {
    $description: 'Generated by `npm run tokens:build` from design/tokens.json. Figma mirrors the tokens and never owns them.',
    collections: [
      { name: 'Primitives', modes: ['Value'], variables: primitives },
      { name: 'Semantic', modes: ['Light', 'Dark'], variables: semantic },
      { name: 'Component', modes: ['Value'], variables: component },
    ],
    effects,
  }
  return JSON.stringify(doc, null, 2) + '\n'
}

// --- All outputs -------------------------------------------------------------

/** Every generated file, by its path relative to the dashboard folder. */
export function buildOutputs(tree = loadTokens()) {
  const m = model(tree)
  return new Map([
    ['src/tokens.css', tokensCss(m)],
    ['ui-kit/otodock-tokens.css', kitCss(m)],
    ['src/uikit/tokens.generated.ts', widgetConstants(m)],
    ['design/figma-variables.json', figmaVariables(m)],
    ['design/reference/index.html', referencePage(m)],
  ])
}

/** The reference page, from the model and the primitives' class strings. */
export function referencePage(m) {
  const ui = join(DASHBOARD, 'src', 'components', 'ui')
  return referenceHtml(m, {
    rows: measureContrast(m),
    classes: readClassStrings(join(ui, 'classes.ts')),
    glass: readClassStrings(join(ui, 'glass.ts')),
    h: { cssColor, directRef, themeKey },
  })
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const outputs = buildOutputs()
  for (const [rel, text] of outputs) {
    const path = join(DASHBOARD, rel)
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, text)
    process.stdout.write(`wrote ${rel}\n`)
  }
  // The page's component CSS comes from Tailwind's compiler (async, so it is
  // not in buildOutputs); the suite rebuilds it the same way and compares.
  const css = await referenceCss(outputs.get('design/reference/index.html'), DASHBOARD)
  writeFileSync(join(DASHBOARD, 'design', 'reference', 'components.css'), css)
  process.stdout.write('wrote design/reference/components.css\n')
}
