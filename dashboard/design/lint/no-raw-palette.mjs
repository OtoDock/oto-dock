// otodock/no-raw-palette: the dashboard paints with the semantic tokens
// (src/tokens.css), never with Tailwind's palette or a literal colour.
//
// Every string literal and template quasi is read, wherever it sits (a
// className, a ternary, an object map, a constant), because most colour
// classes live outside JSX attributes. Each whitespace-separated word is
// stripped of its variants (`dark:`, `hover:`, `[&>svg]:`), a leading `!`
// or `-`, a trailing `!` and its `/opacity`, then reported when it is a
// palette utility (`text-amber-700`), a white or black utility
// (`bg-white`), or an arbitrary colour (`bg-[#f4b206]`). A hex colour
// anywhere in a string (`'#146bb5'`, `color:#333`) is reported too; an
// all-digit match counts only where it must be a colour, so "PR #123" is
// left alone.

const HUES =
  'amber|yellow|green|emerald|red|rose|blue|sky|indigo|purple|violet|teal|cyan|' +
  'gray|slate|zinc|neutral|stone|orange|lime|pink|fuchsia'
const PREFIX =
  'text|bg|border|border-[trblxyse]|ring|ring-offset|inset-ring|from|via|to|fill|stroke|outline|' +
  'shadow|inset-shadow|drop-shadow|text-shadow|decoration|divide|placeholder|caret|accent'

const PALETTE = new RegExp(`^(?:${PREFIX})-(?:${HUES})-(?:50|[1-9]00|950)$`)
const WHITE_BLACK = new RegExp(`^(?:${PREFIX})-(?:white|black)$`)
const ARBITRARY = new RegExp(
  `^(?:${PREFIX})-\\[[^\\]]*(?:#[0-9a-f]{3,8}\\b|rgba?\\(|hsla?\\(|oklch\\(|oklab\\(|color:)`,
  'i',
)
const HEX = /(^|[\s:(,])#([0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})(?![0-9a-z_-])/gi

/** A class word without its variants, important marks and opacity. */
export function bareUtility(word) {
  let depth = 0
  let cut = 0
  for (let i = 0; i < word.length; i++) {
    const ch = word[i]
    if (ch === '[' || ch === '(') depth++
    else if (ch === ']' || ch === ')') depth--
    else if (ch === ':' && depth === 0) cut = i + 1
  }
  let u = word.slice(cut).replace(/^[!-]+/, '').replace(/!$/, '')
  u = u.replace(/\/(?:\d+|\[[^\]]*\])$/, '')
  return u
}

/** What is wrong with one class word, or null. */
export function classProblem(word) {
  if (!word || word.length > 200) return null
  const u = bareUtility(word)
  if (PALETTE.test(u)) return 'palette'
  if (WHITE_BLACK.test(u)) return 'whiteBlack'
  if (ARBITRARY.test(u)) return 'arbitrary'
  return null
}

/** The hex colours a string names (an all-digit one only where it must be
 *  a colour: the whole string, or after `:`, `(` or `,`). */
export function hexColours(text) {
  const out = []
  for (const m of text.matchAll(HEX)) {
    const digitsOnly = /^\d+$/.test(m[2])
    if (digitsOnly && text.trim() !== `#${m[2]}` && ![':', '(', ','].includes(m[1])) continue
    out.push(`#${m[2]}`)
  }
  return out
}

/** Every problem in one string: `[word, kind]` pairs. */
export function stringProblems(text) {
  const found = []
  const seen = new Set()
  for (const word of text.split(/\s+/)) {
    const kind = classProblem(word)
    if (kind) found.push([word, kind])
    if (kind === 'arbitrary') seen.add(word)
  }
  for (const hex of hexColours(text)) {
    if ([...seen].some((w) => w.includes(hex))) continue
    found.push([hex, 'hex'])
  }
  return found
}

const rule = {
  meta: {
    type: 'problem',
    docs: { description: 'Paint with the semantic tokens, never with raw palette colours' },
    schema: [],
    messages: {
      palette:
        '"{{word}}" is a raw palette colour: use the semantic token for its meaning ' +
        '(text-primary, bg-surface-card, border-subtle, bg-status-warning-bg, text-accent-agent-fg, …).',
      whiteBlack:
        '"{{word}}" paints a fixed white or black: use the token for its role ' +
        '(text-action-fg on an action fill, bg-surface-card, text-inverse).',
      arbitrary: '"{{word}}" is an arbitrary colour: use a semantic token instead of a literal.',
      hex: '"{{word}}" is a literal colour: read it from the tokens (a semantic class, or var(--color-…)).',
    },
  },
  create(context) {
    const check = (node, text) => {
      if (typeof text !== 'string' || text.length === 0) return
      for (const [word, kind] of stringProblems(text)) {
        context.report({ node, messageId: kind, data: { word } })
      }
    }
    return {
      Literal(node) {
        check(node, node.value)
      },
      TemplateElement(node) {
        check(node, node.value.cooked)
      },
    }
  },
}

export default rule
