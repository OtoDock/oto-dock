// The colour codemod: rewrites recurring palette class recipes to the
// semantic tokens, file by file, and prints what it could not map.
//
//   node design/codemod.mjs [--write] <file or folder>…
//
// Without --write it only reports. Every string literal and template part
// is read (TypeScript's own parser finds them). In each string:
//   1. known multi-class recipes (the glass strings, the card) are replaced
//      whole;
//   2. a light palette class and its `dark:` partner for the same property
//      become one semantic class (`text-amber-700 dark:text-amber-400` →
//      `text-status-warning-fg`); a light class alone maps the same way when
//      no `dark:` class sets that property;
//   3. the alias names are renamed (`text-p-text-light` → `text-tertiary`,
//      `bg-brand` → `bg-action`);
//   4. what is left (a lone `dark:` palette class, blue, `text-brand`,
//      `text-white` away from a fill) is reported, never guessed.
// Review the diff: the table maps meaning by hue, and a hue used against its
// meaning (green for "selected") needs a person.
import { readFileSync, writeFileSync, statSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

import { bareUtility, classProblem } from './lint/no-raw-palette.mjs'

const FAMILY = {
  amber: 'status-warning', yellow: 'status-warning', orange: 'status-warning',
  red: 'status-danger', rose: 'status-danger',
  green: 'status-success', emerald: 'status-success',
  purple: 'accent-agent', violet: 'accent-agent',
  teal: 'accent-media', cyan: 'accent-media',
  gray: 'neutral', slate: 'neutral', zinc: 'neutral', neutral: 'neutral', stone: 'neutral',
}

// Whole recipes, replaced before anything else (longest first).
export const RECIPES = [
  ['bg-white/70 dark:bg-gray-900/70 backdrop-blur-xs border border-p-border-light/50 dark:border-gray-600 hover:bg-white/90 dark:hover:bg-gray-800/90',
    'bg-glass-fill backdrop-blur-xs border border-glass-border hover:bg-glass-hover'],
  ['bg-white/80 dark:bg-gray-900/80 backdrop-blur-xs border border-p-border-light', 'bg-glass-fill backdrop-blur-xs border border-glass-border'],
  ['bg-white/95 dark:bg-gray-900/95 backdrop-blur-xs border border-p-border-light', 'bg-glass-fill backdrop-blur-xs border border-glass-border'],
  ['bg-white/90 dark:bg-gray-900/90 backdrop-blur-xs', 'bg-glass-fill backdrop-blur-xs'],
  ['bg-white dark:bg-p-surface', 'bg-surface-card'],
  ['bg-white dark:bg-gray-800', 'bg-surface-card'],
  ['bg-white dark:bg-gray-900', 'bg-surface-card'],
  ['bg-[#f4b206]/10', 'bg-status-warning-bg'],
  ['border-[#f4b206]/30', 'border-status-warning-border'],
  ['text-[#b8860b]', 'text-status-warning-fg'],
]

// The alias names and their successors (any variant, any opacity).
// `text-brand` is absent on purpose: it is a link or an accent.
export const RENAMES = {
  'bg-p-bg': 'bg-surface-page',
  'bg-p-surface': 'bg-surface-muted',
  'bg-p-surface-hover': 'bg-surface-hover',
  'bg-p-bg-hover': 'bg-surface-hover',
  'bg-p-bg-secondary': 'bg-surface-hover',
  'text-p-text': 'text-primary',
  'text-p-header': 'text-primary',
  'text-p-text-secondary': 'text-secondary',
  'text-p-text-light': 'text-tertiary',
  'border-p-border-light': 'border-subtle',
  'border-p-border': 'border-default',
  'divide-p-border-light': 'divide-subtle',
  'divide-p-border': 'divide-default',
  'ring-offset-p-bg': 'ring-offset-surface-page',
  'bg-brand': 'bg-action',
  'bg-brand-hover': 'bg-action-hover',
  'bg-brand-surface': 'bg-action-subtle',
  'bg-brand-50': 'bg-action-subtle',
  'bg-brand-100': 'bg-action-subtle-hover',
  'text-brand-hover': 'text-link-hover',
  'border-brand': 'border-action',
  'ring-brand': 'ring-focus-ring',
  'text-p-accent-red': 'text-status-danger-fg',
  'text-p-error': 'text-status-danger-fg',
  'text-p-success': 'text-status-success-fg',
  'text-p-accent-yellow': 'text-status-warning-fg',
  'text-p-accent-purple-text': 'text-accent-agent-fg',
  'text-p-accent-teal': 'text-accent-media-fg',
  'bg-p-accent-red': 'bg-status-danger-fill',
  'bg-p-error': 'bg-status-danger-fill',
  'bg-p-accent-purple': 'bg-accent-agent-fill',
  'bg-p-accent-teal': 'bg-accent-media-solid',
  'bg-p-accent-yellow': 'bg-status-warning-solid',
  'bg-p-success': 'bg-status-success-solid',
  'border-p-accent-teal': 'border-accent-media-solid',
  'border-p-accent-purple': 'border-accent-agent-solid',
  'border-p-accent-red': 'border-status-danger-solid',
  'ring-p-accent-teal': 'ring-accent-media-solid',
  'ring-p-accent-purple': 'ring-accent-agent-solid',
}

const PALETTE = /^(text|bg|border|border-[trblxyse]|ring|divide|placeholder)-([a-z]+)-(50|[1-9]00|950)$/

/** Split a class word into its variant prefix, the utility and its
 *  opacity suffix: `dark:hover:bg-amber-900/30` → ['dark:hover:', 'bg-amber-900', '/30']. */
export function splitClass(word) {
  const bare = bareUtility(word)
  const at = word.lastIndexOf(bare)
  const prefix = word.slice(0, at)
  const suffix = word.slice(at + bare.length)
  return [prefix, bare, suffix]
}

/** The semantic class for one light palette utility, or null. */
export function semanticFor(utility) {
  if (utility === 'bg-white') return 'bg-surface-card'
  const m = PALETTE.exec(utility)
  if (!m) return null
  const [, prop, hue, shadeText] = m
  const family = FAMILY[hue]
  if (!family) return null
  const shade = Number(shadeText)
  const p = prop
  if (family === 'neutral') {
    if (prop === 'text' || prop === 'placeholder') {
      if (shade >= 800) return `${p}-primary`
      if (shade >= 600) return `${p}-secondary`
      return `${p}-tertiary`
    }
    if (prop === 'bg') return shade <= 50 ? 'bg-surface-sunken' : shade <= 200 ? 'bg-surface-muted' : null
    if (prop.startsWith('border') || prop === 'divide') return shade <= 100 ? `${p}-subtle` : shade <= 300 ? `${p}-default` : `${p}-strong`
    return null
  }
  if (prop === 'text') return `text-${family}-fg`
  if (prop === 'bg') {
    if (shade <= 100) return `bg-${family}-bg`
    if (shade === 200) return `bg-${family}-border`
    if (family === 'status-danger' && shade >= 600) return shade >= 700 ? 'bg-status-danger-fill-hover' : 'bg-status-danger-fill'
    if (family === 'accent-agent' && shade >= 600) return 'bg-accent-agent-fill'
    return `bg-${family}-solid`
  }
  if (prop.startsWith('border') || prop === 'ring' || prop === 'divide') {
    return shade <= 400 ? `${p}-${family}-border` : `${p}-${family}-solid`
  }
  return null
}

const PROPERTY = (utility) => {
  const m = /^(text|bg|border(?:-[trblxyse])?|ring|divide|placeholder)-/.exec(utility)
  return m ? m[1] : null
}

/** Rewrite one string's classes; returns [newText, changes, unmapped]. */
export function rewriteClasses(text) {
  let out = text
  let changes = 0
  for (const [from, to] of RECIPES) {
    if (out.includes(from)) {
      out = out.split(from).join(to)
      changes++
    }
  }
  const parts = out.split(/(\s+)/)
  const words = parts.map((w, i) => (i % 2 === 0 ? w : null))
  const unmapped = []
  const dropped = new Set()
  // Pairs and singles.
  for (let i = 0; i < words.length; i++) {
    const w = words[i]
    if (!w || dropped.has(i)) continue
    const [prefix, util, suffix] = splitClass(w)
    if (prefix.includes('dark:')) continue
    const target = semanticFor(util)
    if (!target) continue
    const prop = PROPERTY(util)
    const partner = words.findIndex((x, j) => {
      if (!x || j === i || dropped.has(j)) return false
      const [pp, uu] = splitClass(x)
      return pp === `dark:${prefix}` && PROPERTY(uu) === prop
    })
    if (partner >= 0) {
      const [, du] = splitClass(words[partner])
      const pm = PALETTE.exec(du)
      const lightFamily = util === 'bg-white' ? 'neutral' : FAMILY[PALETTE.exec(util)[2]]
      const sameFamily = pm && FAMILY[pm[2]] && FAMILY[pm[2]] === lightFamily
      const darkIsToken = !pm && /^(bg|text|border)-(p-|surface|status|accent)/.test(du)
      if (!sameFamily && !darkIsToken) continue
      dropped.add(partner)
    }
    // A faint tint of a solid hue is the pill background.
    const faint = /^\/(\d+)$/.exec(suffix)
    const tint = target.endsWith('-solid') && target.startsWith('bg-') && faint && Number(faint[1]) <= 25
    const finalTarget = tint ? target.replace(/-solid$/, '-bg') : target
    const keepOpacity = finalTarget.startsWith('bg-') && suffix && !tint && !/-bg$/.test(finalTarget) ? suffix : ''
    words[i] = `${prefix}${finalTarget}${keepOpacity}`
    changes++
  }
  // Black at an opacity: a hover tint or a scrim.
  for (let i = 0; i < words.length; i++) {
    const w = words[i]
    if (!w || dropped.has(i)) continue
    const [prefix, util, suffix] = splitClass(w)
    const alpha = /^\/(\d+)$/.exec(suffix)
    if (util !== 'bg-black' || !alpha || prefix.includes('dark:')) continue
    const n = Number(alpha[1])
    if (n > 10 && n < 30) continue
    words[i] = `${prefix}${n <= 10 ? 'bg-surface-hover' : 'bg-surface-scrim'}`
    const partner = words.findIndex((x, j) => x && j !== i && splitClass(x)[0] === `dark:${prefix}` && splitClass(x)[1] === 'bg-white')
    if (partner >= 0) dropped.add(partner)
    changes++
  }
  // Renames.
  for (let i = 0; i < words.length; i++) {
    const w = words[i]
    if (!w || dropped.has(i)) continue
    const [prefix, util, suffix] = splitClass(w)
    const to = RENAMES[util]
    if (to) {
      words[i] = `${prefix}${to}${suffix}`
      changes++
    }
  }
  // `text-white` on a fill the string also names.
  const joined = words.filter((w, i) => w && !dropped.has(i)).join(' ')
  for (let i = 0; i < words.length; i++) {
    const w = words[i]
    if (!w || dropped.has(i)) continue
    const [prefix, util, suffix] = splitClass(w)
    if (util !== 'text-white') continue
    if (/\bbg-action\b/.test(joined)) words[i] = `${prefix}text-action-fg${suffix}`
    else if (/\bbg-(status-[a-z]+-(fill|solid)|accent-[a-z]+-(fill|solid))\b/.test(joined)) words[i] = `${prefix}text-on-solid${suffix}`
    else continue
    changes++
  }
  // A dropped word takes the separator before it (after it at the start).
  const rebuilt = parts.map((p, i) => (i % 2 === 0 ? words[i] : p))
  for (const i of dropped) {
    rebuilt[i] = ''
    if (i > 0) rebuilt[i - 1] = ''
    else if (i + 1 < rebuilt.length) rebuilt[i + 1] = ''
  }
  const result = rebuilt.join('')
  for (const w of result.split(/\s+/)) {
    if (!w) continue
    const [, util] = splitClass(w)
    if (classProblem(w) || util === 'text-brand' || util === 'text-brand-hover') unmapped.push(w)
  }
  return [changes ? result : text, changes, unmapped]
}

/** Every string literal and template part of a file, with its span. */
function stringSpans(source, fileName) {
  const sf = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true,
    fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const spans = []
  const visit = (node) => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      spans.push([node.getStart(sf) + 1, node.getEnd() - 1])
    } else if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
      const start = node.getStart(sf) + 1
      const end = node.getEnd() - (ts.isTemplateTail(node) ? 1 : 2)
      spans.push([start, end])
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return spans
}

/** Rewrite a whole file's text; returns [newText, changes, unmapped lines]. */
export function rewriteSource(source, fileName = 'x.tsx') {
  let out = ''
  let last = 0
  let changes = 0
  const unmapped = []
  for (const [start, end] of stringSpans(source, fileName)) {
    const [text, n, left] = rewriteClasses(source.slice(start, end))
    out += source.slice(last, start) + text
    last = end
    changes += n
    if (left.length) {
      const line = source.slice(0, start).split('\n').length
      unmapped.push(`${line}: ${left.join(' ')}`)
    }
  }
  out += source.slice(last)
  return [out, changes, unmapped]
}

function files(path, out = []) {
  if (statSync(path).isDirectory()) {
    for (const e of readdirSync(path)) files(join(path, e), out)
  } else if (/\.(ts|tsx)$/.test(path) && !/\.d\.ts$/.test(path)) {
    out.push(path)
  }
  return out
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const args = process.argv.slice(2)
  const write = args.includes('--write')
  let total = 0
  let left = 0
  for (const path of args.filter((a) => a !== '--write').flatMap((a) => files(a))) {
    const source = readFileSync(path, 'utf8')
    const [text, changes, unmapped] = rewriteSource(source, path)
    total += changes
    left += unmapped.length
    if (!changes && !unmapped.length) continue
    process.stdout.write(`${path}: ${changes} rewritten${unmapped.length ? `, ${unmapped.length} strings left` : ''}\n`)
    for (const u of unmapped) process.stdout.write(`  ${u}\n`)
    if (write && changes) writeFileSync(path, text)
  }
  process.stdout.write(`${total} rewritten, ${left} strings with classes left to a person${write ? '' : ' (dry run; --write applies)'}\n`)
}
