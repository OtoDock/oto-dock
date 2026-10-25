// The reference page (design/reference/index.html): every token in both
// themes with its primitive and contrast, the type, the radius, elevation
// and motion scales, and the primitives drawn with their real class
// strings (read from components/ui/classes.ts and glass.ts, never from
// rendered components). The page is generated from the model the token
// generator builds; its component CSS (components.css) is compiled by
// Tailwind for exactly the classes the page names, as a separate step.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import ts from 'typescript'
import { inlineMark } from './brand/export.mjs'

const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

/** The exported class strings of a module: `export const X = '…'` (string
 *  concatenations included), `export const X = { k: '…' }`, and a reference
 *  to an earlier export (`X` or `X.k`). */
export function readClassStrings(path) {
  const source = readFileSync(path, 'utf8')
  const sf = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const out = {}
  const str = (node) => {
    if (ts.isAsExpression(node) || ts.isParenthesizedExpression(node)) return str(node.expression)
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text
    if (ts.isIdentifier(node)) return typeof out[node.text] === 'string' ? out[node.text] : null
    if (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression)) {
      const v = out[node.expression.text]?.[node.name.text]
      return typeof v === 'string' ? v : null
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      const a = str(node.left)
      const b = str(node.right)
      return a === null || b === null ? null : a + b
    }
    return null
  }
  for (const st of sf.statements) {
    if (!ts.isVariableStatement(st) || !st.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword)) continue
    for (const d of st.declarationList.declarations) {
      let init = d.initializer
      while (init && (ts.isAsExpression(init) || ts.isParenthesizedExpression(init))) init = init.expression
      if (!init) continue
      if (ts.isObjectLiteralExpression(init)) {
        const obj = {}
        for (const p of init.properties) {
          if (!ts.isPropertyAssignment(p)) continue
          const v = str(p.initializer)
          if (v === null) throw new Error(`${path}: ${d.name.getText(sf)}.${p.name.getText(sf)} is not a class string`)
          obj[p.name.getText(sf)] = v
        }
        out[d.name.getText(sf)] = obj
      } else {
        const s = str(init)
        if (s !== null) out[d.name.getText(sf)] = s
      }
    }
  }
  return out
}

const MODES = ['light', 'dark']
const LABEL = { light: 'Light', dark: 'Dark' }

function section(id, title, body, intro = '') {
  return `<section id="${id}"><h2>${esc(title)}</h2>${intro ? `<p class="intro">${intro}</p>` : ''}${body}</section>`
}

function bothModes(render) {
  return `<div class="modes">${MODES.map((mode) => `<div class="mode ${mode === 'dark' ? 'dark' : 'light'}"><h3 class="mode-name">${LABEL[mode]}</h3>${render(mode)}</div>`).join('')}</div>`
}

function colourRows(m, h) {
  const groups = new Map()
  for (const s of m.semantic) {
    const g = s.path[0]
    if (!groups.has(g)) groups.set(g, [])
    groups.get(g).push(s)
  }
  let html = ''
  for (const [group, list] of groups) {
    html += `<h3>${esc(group)}</h3><table class="swatches"><thead><tr><th>Token</th><th>Utility key</th><th>Light</th><th>Dark</th></tr></thead><tbody>`
    for (const s of list) {
      const cell = (mode) => {
        const prim = h.directRef(m.tokens, s.name, mode)
        const value = h.cssColor(s[mode])
        return `<td><span class="chip" style="background:${value}"></span><code>${esc(value)}</code>${prim ? `<br><small>${esc(prim)}</small>` : ''}</td>`
      }
      html += `<tr><td><code>${esc(s.name)}</code>${s.description ? `<br><small>${esc(s.description)}</small>` : ''}</td><td><code>${esc(h.themeKey(s.path))}</code></td>${cell('light')}${cell('dark')}</tr>`
    }
    html += '</tbody></table>'
  }
  return html
}

function primitiveSwatches(m, h) {
  const byGroup = new Map()
  for (const [name, tok] of m.tokens) {
    if (tok.path[0] !== 'color') continue
    const g = tok.path[1]
    if (!byGroup.has(g)) byGroup.set(g, [])
    byGroup.get(g).push([name, tok.value])
  }
  let html = '<div class="primitives">'
  for (const [g, list] of byGroup) {
    html += `<div class="ramp"><div class="ramp-name">${esc(g)}</div>`
    for (const [name, value] of list) {
      html += `<div class="ramp-step"><span class="chip big" style="background:${h.cssColor(value)}"></span><small>${esc(name.split('.').slice(2).join('.') || g)}<br>${esc(value)}</small></div>`
    }
    html += '</div>'
  }
  return html + '</div>'
}

function contrastTable(rows) {
  const keyed = new Map()
  for (const r of rows) {
    const key = `${r.fg}|${r.bg}|${r.over ?? ''}`
    if (!keyed.has(key)) keyed.set(key, { ...r, light: null, dark: null })
    keyed.get(key)[r.mode] = r
  }
  let html = '<table class="contrast"><thead><tr><th>Foreground</th><th>Background</th><th>Needs</th><th>Light</th><th>Dark</th></tr></thead><tbody>'
  for (const r of keyed.values()) {
    const cell = (x) => `<td class="${x.pass ? 'pass' : 'fail'}">${x.ratio.toFixed(2)}</td>`
    html += `<tr><td><code>${esc(r.fg)}</code></td><td><code>${esc(r.bg)}</code>${r.over ? ` <small>over ${esc(r.over)}</small>` : ''}</td><td>${r.min}:1</td>${cell(r.light)}${cell(r.dark)}</tr>`
  }
  return html + '</tbody></table>'
}

function typeSection(m) {
  const family = (n) => m.fontFamily.find((f) => f.name === n)
  const specimen = (n, sample, weights) => {
    const f = family(n)
    return `<div class="specimen"><div class="specimen-head"><code>font-${n}</code> <small>${esc(f.value)}</small><br><small>${esc(f.description ?? '')}</small></div>${weights.map((w) => `<p style="font-family:${esc(f.value)};font-weight:${w};font-size:22px">${sample} <small>${w}</small></p>`).join('')}</div>`
  }
  let html = specimen('display', 'OtoDock Agents Αυτοματισμοί', [600, 700])
  html += specimen('sans', 'The quick brown fox jumps over the lazy dog. Γρήγορη καφέ αλεπού.', [400, 500, 600, 700])
  html += specimen('mono', 'const agent = await otodock.run("brief") // 0O 1lI', [400, 700])
  html += '<table class="scale"><thead><tr><th>Utility</th><th>Size</th><th>Line height</th><th>Sample</th></tr></thead><tbody>'
  for (const s of m.fontSize) {
    html += `<tr><td><code>text-${s.name}</code></td><td>${s.value}</td><td>${s.lineHeight}</td><td style="font-size:${s.value};line-height:${s.lineHeight}">Plain words read at every size</td></tr>`
  }
  return html + '</tbody></table>'
}

function markSection() {
  // The states and the sizes each serves: the small optical mark at 16 and
  // 24, the flat mark from 32 up and in every icon, the glass states at
  // hero sizes only (never below 96).
  const rows = [
    ['flat', 'The mark on light surfaces, every size from 32 px, every icon', [32, 48, 96, 192]],
    ['white', 'White on dark and hero surfaces', [32, 48, 96, 192]],
    ['small', 'The small optical mark: a disc for the gear, 16 and 24 px', [16, 24]],
    ['glass', 'The lit state, 96 px and up on 2x displays', [96, 192]],
    ['white-glass', 'The lit state on dark and hero surfaces', [96, 192]],
  ]
  let html = '<p class="intro">Every state of the master mark (<code>design/brand/otodock-mark.svg</code>) at the sizes it serves.</p><div class="marks">'
  for (const [state, note, sizes] of rows) {
    html += `<div class="mark-row"><div class="mark-state"><code>${state}</code><br><small>${note}</small></div>`
    for (const s of sizes) html += `<div class="mark-frame ${state}" title="${s} px">${inlineMark({ state, size: s, title: '' })}</div><small>${s}</small>`
    html += '</div>'
  }
  return html + '</div>'
}

const ICONS = [
  ['chat', '<path d="M4 5h16v10H8l-4 4z"/>'],
  ['settings', '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/>'],
  ['agents', '<circle cx="9" cy="8" r="3"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><path d="M17 11a3 3 0 1 0-1-5.8M21 20c0-2.6-1.7-4.8-4-5.6"/>'],
  ['bell', '<path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>'],
  ['file', '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>'],
]

function iconSection() {
  let html = '<p class="intro">Inline outline SVGs in the dashboard\'s own style: a 24-unit box, stroke 1.75, round caps and joins, <code>currentColor</code>, shown at 16 and 20 px. No icon package.</p><div class="icons">'
  for (const [name, path] of ICONS) {
    for (const size of [16, 20]) {
      html += `<span class="icon"><svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${path}</svg><small>${name} ${size}</small></span>`
    }
  }
  return html + '</div>'
}

function componentsSection(c, g) {
  const btn = (variant, size, label) => `<button type="button" class="${esc(`${c.BUTTON_BASE} ${c.BUTTON_VARIANT[variant]} ${c.BUTTON_SIZE[size]}`)}">${label}</button>`
  const render = () => {
    let html = '<div class="demo"><h4>Buttons</h4>'
    for (const variant of Object.keys(c.BUTTON_VARIANT)) {
      html += `<div class="row">${Object.keys(c.BUTTON_SIZE).map((size) => btn(variant, size, `${variant} ${size}`)).join('')}</div>`
    }
    html += '</div><div class="demo"><h4>Fields</h4>'
    html += `<label class="${esc(c.LABEL)}">Agent name</label><input class="${esc(`${c.FIELD} ${c.FIELD_REGULAR} ${c.FIELD_BORDER}`)}" placeholder="Placeholder text">`
    html += `<div class="gap"></div><input class="${esc(`${c.FIELD} ${c.FIELD_COMPACT} ${c.FIELD_BORDER}`)}" value="Compact">`
    html += `<div class="gap"></div><input class="${esc(`${c.FIELD} ${c.FIELD_REGULAR} ${c.FIELD_INVALID}`)}" value="Invalid" aria-invalid="true">`
    html += `<div class="gap"></div><select class="${esc(`${c.FIELD} ${c.FIELD_REGULAR} ${c.FIELD_BORDER}`)}"><option>A native select</option></select></div>`
    html += '<div class="demo"><h4>Switch</h4><div class="row">'
    for (const on of [true, false]) {
      html += `<button type="button" role="switch" aria-checked="${on}" class="${esc(`${c.SWITCH_TRACK} ${on ? c.SWITCH_ON : c.SWITCH_OFF}`)}"><span class="${esc(`${c.SWITCH_THUMB} ${on ? 'translate-x-4' : 'translate-x-0'}`)}"></span></button><small>${on ? 'on' : 'off'}</small>`
    }
    html += '</div></div><div class="demo"><h4>Badges by meaning</h4><div class="row">'
    for (const tone of Object.keys(c.BADGE_TONE)) html += `<span class="${esc(`${c.BADGE_BASE} ${c.BADGE_TONE[tone]}`)}">${tone}</span>`
    html += '</div></div><div class="demo"><h4>Card</h4>'
    html += `<div class="${esc(c.CARD)} pad">A card: the surface step and elevation 1 set it apart, no border.</div><div class="gap"></div>`
    html += `<div class="${esc(c.CARD)} pad">A group: a card holding its items.<div class="gap"></div><div class="${esc(c.CARD_BORDERED)} pad">An item inside a group: the page's own colour and a hairline.</div></div></div>`
    html += '<div class="demo"><h4>Segmented control</h4><div class="row" role="group" aria-label="View">'
    for (const [label, chosen] of [['Map', false], ['Grid', true], ['Departments', false]]) {
      html += `<button type="button" aria-pressed="${chosen}" class="${esc(`${c.BUTTON_BASE} ${c.BUTTON_SIZE.sm} ${chosen ? c.SEGMENT.on : c.SEGMENT.off}`)}">${label}</button>`
    }
    html += '</div></div>'
    html += `<div class="demo"><h4>Dialog panel</h4><div class="${esc(`${c.DIALOG_PANEL} max-w-md`)}"><div class="pad"><strong>Remove this agent?</strong><p class="text-secondary">Short sentences. One idea each.</p><div class="row end">${btn('secondary', 'sm', 'Cancel')}${btn('danger', 'sm', 'Remove')}</div></div></div></div>`
    html += `<div class="demo"><h4>Popover and menu</h4><div class="${esc(c.POPOVER)} menu">${['Rename', 'Move', 'Share'].map((x) => `<button type="button" class="${esc(c.MENU_ITEM)}">${x}</button>`).join('')}</div></div>`
    html += `<div class="demo"><h4>Tooltip and toast</h4><span class="${esc(c.TOOLTIP)}">A tooltip on the inverse surface</span><div class="gap"></div><div class="${esc(c.TOAST)} pad">A toast on the overlay surface, elevation 3.</div></div>`
    const infoGlyph = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/></svg>'
    html += `<div class="demo"><h4>Info button</h4><div class="text-sm font-medium text-primary">Subscription usage alerts<button type="button" class="${esc(c.INFO_TIP_BUTTON)}" aria-label="About Subscription usage alerts">${infoGlyph}</button></div><div class="gap"></div><div class="text-sm font-medium text-primary">Open<button type="button" class="${esc(c.INFO_TIP_BUTTON)}" aria-expanded="true" aria-label="About the open state">${infoGlyph}</button></div><div class="gap"></div><div class="${esc(c.POPOVER)} max-w-xs p-3 text-sm leading-normal">The longer description, one hover or tap away, on the overlay surface at body size.</div></div>`
    html += `<div class="demo"><h4>Glass (the chat chrome only)</h4><div class="glass-stage"><span class="${esc(`${g.GLASS} ${g.GLASS_HOVER}`)} glass-pill">Personal Assistant</span><span class="${esc(`${g.GLASS} ${g.GLASS_HOVER}`)} glass-square">⚙</span></div></div>`
    return html
  }
  return bothModes(render)
}

function layoutSection(m) {
  const dims = (prefix) => [...m.tokens.values()].filter((t) => t.path.join('.').startsWith(prefix))
  let html = '<table class="scale"><thead><tr><th>Token</th><th>Value</th></tr></thead><tbody>'
  for (const t of [...dims('control.height'), ...dims('topbar'), ...dims('sidebar'), ...dims('spacing')]) {
    html += `<tr><td><code>${esc(t.path.join('.'))}</code>${t.description ? `<br><small>${esc(t.description)}</small>` : ''}</td><td>${esc(t.value)}</td></tr>`
  }
  html += '</tbody></table><h3>Radius</h3><div class="radii">'
  for (const r of m.radius) html += `<div class="radius-demo" style="border-radius:${r.value === '9999px' ? '9999px' : r.value}"><code>rounded-${r.name === 'none' ? 'none' : r.name}</code><small>${r.value}</small></div>`
  html += '</div><div class="radii">'
  for (const r of m.componentRadius) html += `<div class="radius-demo" style="border-radius:${r.value}"><code>rounded-${r.name}</code><small>${r.value}</small></div>`
  html += '</div><p class="intro">Nesting: an inner element takes the outer radius minus the padding between them.</p>'
  html += '<div class="nest" style="border-radius:12px;padding:6px"><div style="border-radius:6px">12 px outer, 6 px padding, 6 px inner</div></div>'
  return html
}

function elevationSection(m) {
  return bothModes((mode) => `<div class="row">${m.shadows.map((s) => `<div class="elev" style="box-shadow:${esc(s[mode])}"><code>shadow-elevation-${s.path[1]}</code><small>${esc(s.description ?? '')}</small></div>`).join('')}</div>`)
}

function motionSection(m) {
  let html = '<table class="scale"><thead><tr><th>Token</th><th>Value</th><th>Use</th><th></th></tr></thead><tbody>'
  for (const d of m.duration) html += `<tr><td><code>duration-${d.name}</code></td><td>${d.value}</td><td>${esc(d.description ?? '')}</td><td><span class="moving" style="transition-duration:${d.value}"></span></td></tr>`
  for (const e of m.easing) html += `<tr><td><code>ease-${e.name}</code></td><td><code>${e.value}</code></td><td>${esc(e.description ?? '')}</td><td><span class="moving" style="transition-timing-function:${e.value}"></span></td></tr>`
  return html + '</tbody></table><p class="intro">Hover the table to play the motion. Reduced motion is respected: the dots stay put.</p>'
}

const PAGE_CSS = `
:root { color-scheme: light dark; }
body { margin: 0; font-family: var(--font-sans, Manrope, system-ui, sans-serif); background: var(--ot-surface-page); color: var(--ot-text-primary); font-size: 14px; line-height: 1.5; }
header, section { max-width: 1180px; margin: 0 auto; padding: 24px; }
header h1 { font-family: Comfortaa, Manrope, sans-serif; font-weight: 700; margin: 0 0 4px; }
nav a { margin-right: 12px; color: var(--ot-text-link); text-decoration: none; }
h2 { font-family: Comfortaa, Manrope, sans-serif; font-size: 22px; margin: 24px 0 8px; border-bottom: 1px solid var(--ot-border-subtle); padding-bottom: 6px; }
h3 { font-size: 15px; margin: 18px 0 6px; } h4 { margin: 0 0 8px; font-size: 13px; color: var(--ot-text-secondary); }
.intro, small { color: var(--ot-text-secondary); } code { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 12px; }
table { border-collapse: collapse; width: 100%; } th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--ot-border-subtle); vertical-align: top; }
.chip { display: inline-block; width: 18px; height: 18px; border-radius: 4px; vertical-align: middle; margin-right: 6px; border: 1px solid rgb(127 127 127 / 0.35); }
.chip.big { width: 36px; height: 36px; display: block; margin: 0 0 4px; }
.primitives { display: flex; flex-direction: column; gap: 10px; } .ramp { display: flex; gap: 8px; align-items: flex-start; flex-wrap: wrap; }
.ramp-name { width: 70px; font-weight: 600; padding-top: 8px; } .ramp-step { width: 72px; }
.contrast td.pass { color: var(--ot-status-success-fg); } .contrast td.fail { color: var(--ot-status-danger-fg); font-weight: 700; }
.modes { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.mode { background: var(--ot-surface-page); color: var(--ot-text-primary); padding: 16px; border-radius: 12px; border: 1px solid var(--ot-border-default); }
.mode-name { margin-top: 0; }
.demo { margin-bottom: 18px; } .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; } .row.end { justify-content: flex-end; }
.gap { height: 8px; } .pad { padding: 14px 16px; } .menu { width: 180px; padding: 4px 0; }
.glass-stage { padding: 18px; border-radius: 12px; background: repeating-linear-gradient(45deg, var(--ot-surface-muted) 0 10px, var(--ot-surface-page) 10px 20px); display: flex; gap: 8px; }
.glass-pill { display: inline-flex; align-items: center; height: 36px; padding: 0 12px; border-radius: 12px; }
.glass-square { display: inline-flex; align-items: center; justify-content: center; width: 36px; height: 36px; border-radius: 12px; }
.specimen { margin-bottom: 14px; } .specimen p { margin: 4px 0; } p.intro { margin: 0 0 10px; } th, td { padding: 6px 8px; }
.marks { display: flex; flex-direction: column; gap: 12px; } .mark-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.mark-state { width: 240px; } .mark-frame { display: inline-flex; padding: 8px; border-radius: 8px; color: var(--ot-brand-mark); background: var(--ot-surface-page); }
.mark-frame.white, .mark-frame.white-glass { background: var(--ot-hero-bg); }
.icons { display: flex; gap: 14px; flex-wrap: wrap; } .icon { display: inline-flex; flex-direction: column; align-items: center; gap: 4px; width: 64px; color: var(--ot-text-primary); }
.radii { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.radius-demo { width: 110px; height: 70px; background: var(--ot-surface-card); border: 1px solid var(--ot-border-default); display: flex; flex-direction: column; justify-content: center; align-items: center; }
.nest { background: var(--ot-surface-muted); width: 320px; } .nest > div { background: var(--ot-surface-card); padding: 12px; }
.elev { width: 180px; height: 90px; background: var(--ot-surface-card); border-radius: 8px; display: flex; flex-direction: column; justify-content: center; align-items: center; gap: 4px; padding: 6px; text-align: center; }
.moving { display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--ot-action); transition-property: transform; }
table:hover .moving { transform: translateX(60px); }
@media (prefers-reduced-motion: reduce) { table:hover .moving { transform: none; } }
@media print {
  body { background: #fff; } nav { display: none; }
  section { page-break-before: always; padding: 0 12px; } header { padding: 0 12px; }
  .modes { grid-template-columns: 1fr 1fr; } .mode { break-inside: avoid; }
  table { font-size: 11px; } tr { break-inside: avoid; }
}
`

/** The page. `h` carries the generator's helpers (no import cycle). */
export function referenceHtml(m, { rows, classes, glass, h }) {
  const sections = [
    ['colour', 'Colour', colourRows(m, h), 'Every semantic token in both themes, with the primitive it points at. Utilities read the key in the second column: <code>--color-surface-card</code> is <code>bg-surface-card</code>, <code>--text-color-primary</code> is <code>text-primary</code>, <code>--border-color-subtle</code> is <code>border-subtle</code>.'],
    ['primitives', 'Primitives', primitiveSwatches(m, h), 'The raw scales. Nothing paints with them directly: the semantic tokens point at them.'],
    ['contrast', 'Contrast', contrastTable(rows), `Every pair the tokens promise, measured in both themes: ${rows.length} measurements, text at 4.5:1 and boundaries at 3:1. A translucent background is measured over the surface named beside it.`],
    ['type', 'Type', typeSection(m), 'Comfortaa for the wordmark and headings at text-lg and larger, Manrope for every other string, JetBrains Mono for code and the terminal. The faces load from the dashboard\'s installed packages; without them the fallbacks show.'],
    ['mark', 'The mark', markSection()],
    ['icons', 'Iconography', iconSection()],
    ['components', 'Components', componentsSection(classes, glass), 'The primitives drawn with the class strings of <code>components/ui/classes.ts</code> and <code>glass.ts</code>.'],
    ['layout', 'Layout and spacing', layoutSection(m)],
    ['elevation', 'Elevation', elevationSection(m), 'One level per layer: cards 1, raised 2, popovers, menus, tooltips and toasts 3, dialogs 4.'],
    ['motion', 'Motion', motionSection(m)],
  ]
  const fonts = ['manrope/400', 'manrope/500', 'manrope/600', 'manrope/700', 'comfortaa/600', 'comfortaa/700', 'jetbrains-mono/400', 'jetbrains-mono/700']
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OtoDock design reference</title>
<!-- Generated by \`npm run tokens:build\` from design/tokens.json and the primitives' class strings. -->
${fonts.map((f) => `<link rel="stylesheet" href="../../node_modules/@fontsource/${f}.css">`).join('\n')}
<link rel="stylesheet" href="../../src/tokens.css">
<link rel="stylesheet" href="./components.css">
<style>${PAGE_CSS}</style>
</head>
<body>
<header>
<h1>OtoDock design reference</h1>
<p class="intro">The tokens, the type, the scales and the primitives of the dashboard, in both themes. Generated: edit <code>design/tokens.json</code> and run <code>npm run tokens:build</code>. Print it to get the reference document.</p>
<nav>${sections.map(([id, title]) => `<a href="#${id}">${esc(title)}</a>`).join('')}</nav>
</header>
${sections.map(([id, title, body, intro]) => section(id, title, body, intro)).join('\n')}
</body>
</html>
`
}

/** The class words the page names, for Tailwind to compile. */
export function pageCandidates(html) {
  const out = new Set()
  for (const m of html.matchAll(/class="([^"]*)"/g)) for (const w of m[1].replace(/&amp;/g, '&').split(/\s+/)) if (w) out.add(w)
  return [...out].sort()
}

/** components.css: Tailwind's CSS for exactly the classes the page names,
 *  on the generated tokens (no reset, so the page's own styles stand). */
export async function referenceCss(html, dashboardDir) {
  const { compile } = await import('@tailwindcss/node')
  const base = join(dashboardDir, 'design', 'reference')
  const compiler = await compile(
    '@import "tailwindcss/theme" layer(theme);\n@import "tailwindcss/utilities" layer(utilities);\n@import "../../src/tokens.css";\n',
    { base, onDependency() {} },
  )
  return RESET + compiler.build(pageCandidates(html))
}

// The part of Tailwind's reset the drawn primitives rely on, in a layer
// below the utilities (the page's own styles stay unlayered).
const RESET = `@layer properties, theme, base, utilities;
@layer base {
  *, ::before, ::after { box-sizing: border-box; border: 0 solid; margin: 0; padding: 0; }
  button, input, select { font: inherit; color: inherit; background-color: transparent; border-radius: 0; }
  button { cursor: pointer; }
  input::placeholder { opacity: 1; }
}
`
