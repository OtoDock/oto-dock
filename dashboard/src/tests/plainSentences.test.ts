import { describe, it, expect } from 'vitest'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// What a person reads in the dashboard, the app's setup page and the app's
// own strings is plain sentences, on the changelog's reader rules: no
// semicolon, no dash, no arrow naming a place, no sentence over 20 words,
// nothing in brackets, no word for how the platform works inside, and a
// capital where a sentence starts. Class names, inline styles, SVG paths,
// CSS, media queries, terminal colour codes, console lines, a note framed
// for the agent and a lone glyph are not prose, and code a sentence quotes
// (a path, a file name, a key, a command) keeps its own spelling. A line may
// opt out with a `plain-sentences: allow` comment above it, and those
// comments are counted.

const src = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const dashboard = path.dirname(src)
/** The first-run page the app bundles, where a person types their server. */
const SETUP_PAGE = path.join(dashboard, 'public', 'setup.html')
/** The Android app's own strings. Read only where the Android tree is
 *  present: the public cut stages it out with the mobile apps. */
const APP_STRINGS = path.join(dashboard, 'android', 'app', 'src', 'main', 'res', 'values', 'strings.xml')

/** Folders and files whose words answer code, not a person, each with its reason. */
const NOT_FOR_PEOPLE: Record<string, string> = {
  'lib/artifactInteraction.ts': "the agent's framing of an action button, twinned in the proxy",
  'components/agents-map/buildEnvironment.ts': 'GLSL shader source',
}

/** How many `plain-sentences: allow` comments the dashboard carries. */
const ALLOWED_LINES = 1

/** Each rule and the reason its fault gives. */
const RULES = {
  semicolon: 'two sentences, not one with a semicolon',
  dash: 'a comma, a colon or a full stop, never a dash',
  arrow: 'name the place in words, like Setup, Security',
  long: 'one idea per sentence, 20 words at most',
  brackets: 'say it in the sentence, or leave it out',
  mechanism: 'say what the person sees, never how it works inside',
  lowercase: 'a sentence starts with a capital',
} as const

/** The changelog lint's words for how the platform works inside: a person
 *  cannot act on them. The two lists stay the same. */
const MECHANISM_WORDS = [
  'carve', 'floor', 'hook', 'broker', 'probe', 'egress', 'daemon', 'endpoint',
  'handler', 'payload', 'regex', 'mutex', 'socket', 'stdout', 'stderr', 'cgroup',
  'namespace', 'userns', 'asyncio', 'coroutine', 'mixin', 'adapter', 'tombstone',
  'manifest', 'runtime', 'sentinel', 'heuristic', 'idempotent', 'pool',
]
const MECHANISM = new RegExp(`\\b(${MECHANISM_WORDS.join('|')})s?\\b`, 'i')

/** Words a screen keeps although a rule refuses them, each with its reason.
 *  Frozen: a word joins only with a reason a reviewer accepts. */
const KEPT: Record<string, string> = {
  macOS: "Apple's name for its system starts in lower case",
}

/** The longest sentence, in words. */
const MAX_WORDS = 20

const NOT_PROSE_ATTRIBUTES = new Set([
  'className', 'style', 'd', 'points', 'viewBox', 'allow', 'sandbox', 'rel', 'target', 'type',
  'role', 'id', 'key', 'name', 'htmlFor', 'href', 'to', 'src', 'autoComplete', 'inputMode', 'accept',
  'data-testid',
])
/** Attributes and object keys whose text a person reads as it stands, so its
 *  first word opens a sentence. An object's `label` is left out: it is as
 *  often a phrase set inside a sentence (`This app wants to open {label}`). */
const READ_NAMES = new Set([
  'title', 'aria-label', 'aria-description', 'placeholder', 'alt', 'hint', 'description',
  'desc', 'sub', 'intro', 'info', 'empty', 'emptyText', 'message', 'tooltip', 'heading', 'subtitle',
  'caption', 'confirmLabel', 'cancelLabel', 'body', 'note', 'help', 'summary', 'textContent',
])
/** Calls whose first text argument a person reads as it stands. */
const READ_CALLS = /^(toast\w*|show\w*|set\w*(Error|Note|Notice|Message|Msg|Hint|Warning))$/
/** Elements whose text is code: a command or a path keeps its spelling. */
const CODE_ELEMENTS = new Set(['code', 'kbd', 'pre', 'samp'])
/** Elements that sit inside a sentence rather than open one. */
const INLINE_ELEMENTS = new Set(['span', 'b', 'strong', 'em', 'i', 'u', 's', 'a', 'small', 'mark', 'sup', 'sub', 'abbr', 'time', 'q'])
/** Components whose children are read on their own, in a bubble: their text
 *  opens a sentence whatever stands before them on the page. */
const SENTENCE_OPENERS = new Set(['InfoTip'])
const ALLOW = 'plain-sentences: allow'
const QUIET_CALLS = /^(console\.\w+|debugWarn|debugLog|logError|noteFold|logger\.\w+|(window\.)?matchMedia|\w+\.querySelector(All)?|\w+\.closest)$/

/** Code a sentence may quote, which keeps its own spelling: a backticked
 *  span, an address, a path, a dotted name, a key, a flag, a key
 *  combination, a name with a digit, an assignment with no space around its
 *  `=` (`KEY=value`). */
const CODE = new RegExp([
  '`[^`]*`',
  '[^\\s=]+=[^\\s=]+',
  '\\b[a-z][a-z0-9+.-]*:\\/\\/[^\\s)\\]]+',
  '[\\w.~*-]*\\/[\\w.~*{}/-]+',
  '\\b\\w+(?:\\.\\w+)+\\b(?:\\([^)]*\\))?',
  '\\b[A-Za-z0-9]*_\\w*',
  '(?<!\\w)--?[a-z][\\w-]*',
  '\\b(?:Ctrl|Shift|Alt|Cmd)\\+[\\w+]+',
  '\\b[a-z][a-z-]*\\d[\\w.-]*',
].join('|'), 'g')
/** A token of a class list: variants, then a utility. */
const CLASS_TOKEN = /^!?([\w[\]-]+:)*-?(bg|text|border|rounded|flex|grid|gap|p[xytblrse]?|m[xytblrse]?|w|h|min|max|size|items|justify|self|place|content|overflow|truncate|font|leading|tracking|shadow|ring|outline|opacity|z|top|left|right|bottom|inset|transition|duration|ease|delay|animate|translate|scale|rotate|origin|cursor|select|pointer|whitespace|break|line|list|space|divide|col|row|order|object|aspect|backdrop|blur|fill|stroke|sr|group|peer|accent|caret|decoration|underline|appearance|resize|snap|scroll|touch|will|shrink|grow|basis|float|hidden|block|inline|contents|table|absolute|relative|fixed|sticky|static|visible|invisible|italic|uppercase|lowercase|capitalize|antialiased|container|prose|isolate)(-[\w./[\]()%#:,-]+)?$/
const WORD = /^[("'“‘[]*([A-Za-z][A-Za-z'’]*(-[A-Za-z]+)*|\{x\})[)\]"'”’.,:!?…]*$/

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name)
    if (e.isDirectory()) { if (e.name !== 'tests') sourceFiles(p, out) }
    else if (/\.tsx?$/.test(e.name) && !/\.(test|d)\.tsx?$/.test(e.name)) out.push(p)
  }
  return out
}

const SCANNED = sourceFiles(src)
  .map((f) => path.relative(src, f).split(path.sep).join('/'))
  .filter((f) => !Object.keys(NOT_FOR_PEOPLE).some((p) => f.startsWith(p)))

/** Whether `text` reads as words: two words at least, mostly words, and not
 *  a list of class names. A `=` with a space on a side belongs to the
 *  sentence (`0 = off`), one between two characters is code (`KEY=value`). */
function isProse(text: string): boolean {
  if (/[{}<>]/.test(text.replace(/\{x\}/g, ''))) return false
  const tokens = text.split(/\s+/).filter(Boolean)
  const words = tokens.filter((t) => WORD.test(t))
  if (words.length < 2 || words.length / tokens.length < 0.6) return false
  return !tokens.every((t) => CLASS_TOKEN.test(t))
}

/** Whether `text` is a short label that is not prose by count alone: a few
 *  tokens, a word among them, and no class name, so its brackets still count
 *  (`Send (Shift+Enter)`). */
function isShortLabel(text: string): boolean {
  if (/[{}<>]/.test(text.replace(/\{x\}/g, ''))) return false
  const tokens = text.split(/\s+/).filter(Boolean)
  return tokens.length <= 4 && tokens.some((t) => /[A-Za-z]{2}/.test(t)) && !tokens.some((t) => CLASS_TOKEN.test(t))
}

/** A bracket opened after a space or at the start, around a word. */
const BRACKETED = /(^|\s|\})\((?=[^()]*[A-Za-z]{2})[^()]*\)/

/** The rules `raw` breaks, each as `rule: reason`. `opens` says the text
 *  stands where a sentence starts; `code` says it shows code, which keeps its
 *  own words and brackets. */
function fault(raw: string, opens = false, code = false): string[] {
  const text = raw.replace(/&[a-z0-9#]+;/gi, ' ')
  if ((text.match(/[A-Za-z]{2,}/g) ?? []).length < 2) {                 // not two words: only a word in brackets counts
    return !code && BRACKETED.test(text) ? [`brackets: ${RULES.brackets}`] : []
  }
  if (/\x1b|\\u001b|\\x1b/.test(raw)) return []                         // terminal colour codes
  if (/(^|;)\s*[a-z-]+\s*:\s*[^;]+;/.test(text) && !/[A-Z][a-z]+ [a-z]+ [a-z]+/.test(text)) return [] // CSS
  if (/^\[[^\]]*\]$/.test(text.trim())) return []                      // a note framed for the agent
  if (/^\((prefers-[\w-]+|(any-)?(pointer|hover)|(min|max)-(width|height))\s*:/.test(text.trim())) return [] // a media query
  if (/\w\[[a-z-]+(=[^\]]*)?\]|\[[a-z-]+=|:not\(/.test(text)) return []    // a CSS selector
  const found: string[] = []
  if (text.includes(';')) found.push(`semicolon: ${RULES.semicolon}`)
  if (/[—–]/.test(text) || /\w\s-\s\w/.test(text)) found.push(`dash: ${RULES.dash}`)
  if (/[A-Za-z]\s*(→|->)\s*[A-Z][a-z]/.test(text)) found.push(`arrow naming a place: ${RULES.arrow}`)
  if (code) return found
  const prose = isProse(text)
  if (!prose && !isShortLabel(text)) return found
  let words = text.replace(/\s+/g, ' ').trim()
  for (const kept of Object.keys(KEPT)) words = words.split(kept).join('Kept')
  words = words.replace(CODE, 'Code')
  if (!prose) return BRACKETED.test(words) ? [...found, `brackets: ${RULES.brackets}`] : found
  const sentences = words.split(/(?<=[.!?…])\s+/)
  const longest = Math.max(...sentences.map((s) => s.split(' ').length))
  if (longest > MAX_WORDS) found.push(`a sentence of ${longest} words: ${RULES.long}`)
  if (/\([^()]*[A-Za-z]{2}[^()]*\)|\[[^[\]]*[A-Za-z]{2}[^[\]]*\]/.test(words)) found.push(`brackets: ${RULES.brackets}`)
  const inside = words.match(MECHANISM)
  if (inside) found.push(`the word "${inside[0]}": ${RULES.mechanism}`)
  if (sentences.some((s, i) => (i > 0 || opens) && /^["'“‘(]*\p{Ll}/u.test(s))) found.push(`a lower-case start: ${RULES.lowercase}`)
  return found
}

/** Whether a JSX child holds anything a person reads. */
function readable(node: ts.Node): boolean {
  if (ts.isJsxText(node)) return node.text.trim() !== ''
  if (ts.isJsxExpression(node)) {
    if (!node.expression) return false
    return !(ts.isStringLiteral(node.expression) && !node.expression.text.trim())
  }
  if (ts.isJsxElement(node) || ts.isJsxFragment(node)) return node.children.some(readable)
  return false
}

/** Whether a JSX child stands where a sentence starts: nothing readable
 *  comes before it in its element, nor before the inline elements around it. */
function opensSentence(node: ts.Node): boolean {
  let cur = node
  for (;;) {
    const parent = cur.parent
    if (!parent || !(ts.isJsxElement(parent) || ts.isJsxFragment(parent))) return true
    for (const sibling of parent.children) {
      if (sibling === cur) break
      if (readable(sibling)) return false
    }
    if (ts.isJsxElement(parent)) {
      const tag = parent.openingElement.tagName.getText()
      if (SENTENCE_OPENERS.has(tag) || (!INLINE_ELEMENTS.has(tag) && !/^[A-Z]/.test(tag))) return true
    }
    cur = parent
  }
}

/** Whether a node sits inside an element that shows code. */
function insideCode(node: ts.Node): boolean {
  for (let p = node.parent; p; p = p.parent) {
    if (ts.isJsxElement(p) && CODE_ELEMENTS.has(p.openingElement.tagName.getText())) return true
  }
  return false
}

/** Whether a string stands where a person reads it as it is: a read
 *  attribute or key, a read call's text, or a JSX child that opens a sentence. */
function opensAsRead(node: ts.Node, sf: ts.SourceFile): boolean {
  let cur = node
  let p = node.parent
  while (p && (ts.isParenthesizedExpression(p) || ts.isAsExpression(p)
    || (ts.isConditionalExpression(p) && p.condition !== cur)
    || (ts.isBinaryExpression(p) && [ts.SyntaxKind.QuestionQuestionToken, ts.SyntaxKind.BarBarToken].includes(p.operatorToken.kind)))) {
    cur = p
    p = p.parent
  }
  if (p && ts.isJsxExpression(p) && ts.isJsxAttribute(p.parent)) p = p.parent
  if (!p) return false
  if (ts.isJsxAttribute(p)) return READ_NAMES.has(p.name.getText(sf)) || p.name.getText(sf) === 'label'
  if (ts.isPropertyAssignment(p)) return READ_NAMES.has(p.name.getText(sf).replace(/['"]/g, ''))
  if (ts.isJsxExpression(p)) return !insideCode(p) && opensSentence(p)
  if (ts.isCallExpression(p)) return p.arguments[0] === cur && READ_CALLS.test(p.expression.getText(sf).split('.').pop() ?? '')
  if (ts.isBinaryExpression(p) && p.operatorToken.kind === ts.SyntaxKind.EqualsToken && ts.isPropertyAccessExpression(p.left)) {
    return READ_NAMES.has(p.left.name.text)
  }
  return false
}

function faults(file: string, text = readFileSync(path.join(src, file), 'utf8'), firstLine = 0): string[] {
  const lines = text.split('\n')
  const kind = file.endsWith('.tsx') ? ts.ScriptKind.TSX : file.endsWith('.ts') ? ts.ScriptKind.TS : ts.ScriptKind.JS
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind)
  const found: string[] = []
  const check = (node: ts.Node, raw: string, opens: boolean) => {
    const why = fault(raw, opens, insideCode(node))
    if (!why.length) return
    const line = sf.getLineAndCharacterOfPosition(node.getStart()).line
    if (line > 0 && lines[line - 1].includes(ALLOW)) return
    for (const w of why) found.push(`${file}:${firstLine + line + 1}: ${w}: ${raw.replace(/\s+/g, ' ').trim()}`)
  }
  const visit = (node: ts.Node) => {
    if (ts.isJsxAttribute(node) && NOT_PROSE_ATTRIBUTES.has(node.name.getText(sf))) return
    if (ts.isJsxAttribute(node) && /ClassName$/.test(node.name.getText(sf))) return
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) return
    if (ts.isCallExpression(node) && QUIET_CALLS.test(node.expression.getText(sf))) return
    if (ts.isTemplateExpression(node)) {
      // A template is one sentence around its values.
      check(node, node.head.text + node.templateSpans.map((s) => '{x}' + s.literal.text).join(''), opensAsRead(node, sf))
      for (const span of node.templateSpans) visit(span.expression)
      return
    }
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      check(node, node.text, opensAsRead(node, sf))
    } else if (ts.isJsxText(node)) {
      check(node, node.getText(sf), opensSentence(node))
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return found
}

/** The setup page's words: its text, the attributes a person reads and its
 *  script's strings, each at its line in the page. */
function setupPageFaults(html = readFileSync(SETUP_PAGE, 'utf8')): string[] {
  const file = 'public/setup.html'
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const lineOf = (needle: string) => html.slice(0, Math.max(0, html.indexOf(needle))).split('\n').length
  const found: string[] = []
  const add = (raw: string, opens: boolean) => {
    for (const w of fault(raw, opens)) found.push(`${file}:${lineOf(raw.trim())}: ${w}: ${raw.replace(/\s+/g, ' ').trim()}`)
  }
  const walker = doc.createTreeWalker(doc.documentElement, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const parent = n.parentElement
    if (!parent || ['SCRIPT', 'STYLE'].includes(parent.tagName) || !n.textContent?.trim()) continue
    let opens = true
    for (let s = n.previousSibling; s; s = s.previousSibling) if (s.textContent?.trim()) opens = false
    add(n.textContent, opens)
  }
  for (const el of Array.from(doc.querySelectorAll('[title], [placeholder], [aria-label], [alt]'))) {
    for (const name of ['title', 'placeholder', 'aria-label', 'alt']) {
      const value = el.getAttribute(name)
      if (value) add(value, true)
    }
  }
  for (const script of Array.from(doc.querySelectorAll('script'))) {
    const code = script.textContent ?? ''
    found.push(...faults(file, code, lineOf(code.slice(0, 80)) - 1))
  }
  return found
}

/** The Android app's strings a person reads (a `translatable="false"` one is a name or an id). */
function appStringFaults(xml = readFileSync(APP_STRINGS, 'utf8')): string[] {
  const found: string[] = []
  for (const m of xml.matchAll(/<string\b([^>]*)>([\s\S]*?)<\/string>/g)) {
    if (/translatable="false"/.test(m[1])) continue
    const raw = m[2].replace(/\\(['"])/g, '$1')
    const line = xml.slice(0, m.index).split('\n').length
    for (const w of fault(raw, true)) found.push(`android strings.xml:${line}: ${w}: ${raw}`)
  }
  return found
}

const ruleOf = (f: string) => f.split(': ').slice(0, 2).join(': ')

describe('plain sentences in the dashboard', () => {
  it('scans every source file a person reads, the share page and the widgets included', () => {
    expect(SCANNED).toContain('components/apps/AppsOverlay.tsx')
    expect(SCANNED).toContain('pages/admin/SharesPage.tsx')
    expect(SCANNED).toContain('components/ui/AlertDialog.tsx')
    expect(SCANNED).toContain('sharehost/main.ts')
    expect(SCANNED).toContain('uikit/widgets.ts')
    expect(SCANNED).not.toContain('lib/artifactInteraction.ts')
    expect(existsSync(SETUP_PAGE)).toBe(true)
  })

  it('finds a semicolon, a dash and a place arrow, and nothing that is not prose', () => {
    const sample = [
      "const a = 'one; two'",
      'const b = `three — ${a} four`',
      'const c = <p className="x; y" style={{}}>Five six - seven</p>',
      'const t = <b title="Open Setup → Security" aria-label="ten">x</b>',
      '// plain-sentences: allow',
      "const d = 'kept; on purpose'",
      "const e = 'no semicolon here &amp; there'",
      "const f = '—'",
      "const g = 'color: red; margin: 0;'",
      "console.warn('a debug line — for us')",
      "const h = 'version 1.8.0 → 1.9.0'",
      "const i = 'built-in and read-only'",
      "const j = '[The agent reads this note — not a person.]'",
    ].join('\n')
    expect(faults('sample.tsx', sample).map(ruleOf)).toEqual([
      'sample.tsx:1: semicolon', 'sample.tsx:2: dash', 'sample.tsx:3: dash', 'sample.tsx:4: arrow naming a place',
    ])
  })

  it('finds a long sentence, brackets, a word for the inside and a lower-case start, each with its reason', () => {
    // A toast's or an error setter's text opens a sentence, a sentence of 20
    // words passes, a spaced `=` is a word and a `KEY=value` is code, and a
    // short label's brackets count.
    const sample = [
      "const a = 'One two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty more.'",
      "const b = <p>Save the file (it is small).</p>",
      "const c = { title: 'The handler wakes the app.' }",
      "const d = <p>Saved. then it closes.</p>",
      "const e = <label placeholder='the name of the server' />",
      "const f = <p>Run <code>docker compose up -d (once)</code> first.</p>",
      "const g = <p>Open <b>the chat</b> to see it.</p>",
      "const h = { reason: 'not shared with you' }",
      "const i = 'Pick the pool_id in config.env or app.json first.'",
      "const k = 'flex items-center gap-2 text-sm'",
      "const l = 'Agents use gpt-5.1 and o3 here.'",
      'const m = <span> (optional)</span>',
      "const n = { title: 'macOS asks for the permission first.' }",
      "const o = window.matchMedia('(pointer: coarse)')",
      "toastError('the file was not saved')",
      "setError('the name is taken')",
      "const p = 'One two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty.'",
      'const q = <span>Fix rounds (0 = report only)</span>',
      "const r = { title: 'Send (Shift+Enter)' }",
      "const s = 'Set DASHBOARD_PUBLIC_URL=https://a.example in config.env, then restart.'",
      "const u = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex=\"-1\"])'",
    ].join('\n')
    const found = faults('sample.tsx', sample)
    expect(found.map(ruleOf)).toEqual([
      'sample.tsx:1: a sentence of 21 words', 'sample.tsx:2: brackets', 'sample.tsx:3: the word "handler"',
      'sample.tsx:4: a lower-case start', 'sample.tsx:5: a lower-case start', 'sample.tsx:12: brackets',
      'sample.tsx:15: a lower-case start', 'sample.tsx:16: a lower-case start',
      'sample.tsx:18: brackets', 'sample.tsx:19: brackets',
    ])
    expect(found[0]).toContain(RULES.long)
    expect(found[1]).toContain(RULES.brackets)
    expect(found[2]).toContain(RULES.mechanism)
    expect(found[3]).toContain(RULES.lowercase)
  })

  it('reads the setup page and the app strings with the same rules', () => {
    const page = '<html><head><title>Connect to Server</title></head><body><h1>add a server</h1>' +
      '<p title="Opens (soon)">Ok</p><script>\nshowMsg("Validating server — wait.", "info")\n</script></body></html>'
    expect(setupPageFaults(page).map(ruleOf)).toEqual([
      'public/setup.html:1: a lower-case start', 'public/setup.html:1: brackets', 'public/setup.html:2: dash',
    ])
    const xml = '<resources>\n<string name="a">Tap to open the endpoint</string>\n' +
      '<string name="b" translatable="false">com.otodock (id)</string>\n</resources>'
    expect(appStringFaults(xml).map(ruleOf)).toEqual(['android strings.xml:2: the word "endpoint"'])
  })

  it('no text a person reads in the dashboard breaks a rule', () => {
    expect(SCANNED.flatMap((f) => faults(f))).toEqual([])
  })

  it('no word on the setup page breaks a rule', () => {
    expect(setupPageFaults()).toEqual([])
  })

  it.skipIf(!existsSync(APP_STRINGS))("no word in the app's own strings breaks a rule", () => {
    expect(appStringFaults()).toEqual([])
  })

  it('every kept word is still on a screen', () => {
    const all = SCANNED.map((f) => readFileSync(path.join(src, f), 'utf8')).join('\n') + readFileSync(SETUP_PAGE, 'utf8')
    for (const word of Object.keys(KEPT)) expect(all, word).toContain(word)
  })

  it('the opt-out comments do not grow', () => {
    const count = SCANNED.reduce((n, f) => n + readFileSync(path.join(src, f), 'utf8').split(ALLOW).length - 1, 0)
    expect(count).toBe(ALLOWED_LINES)
  })
})
