// @vitest-environment node
// A control that saves on change confirms the save in one place and one
// shape: `SaveState`, driven by `useSaveState`. This reads every source file
// under src/ and fails on a hand-made confirmation outside those two files:
// a JSX text or a string that is exactly "Saved" or "Saved.", a state setter
// named for a saved flag in a file that also clears it with a timer, or an
// import of the two retired badges. A Save button's own "Saving…" is fine.
import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

/** The two files the indication lives in. */
const HOME = ['components/ui/SettingsControls.tsx', 'hooks/useSaveState.ts']

/** Files that keep a confirmation of their own, each with its reason.
 *  Frozen: the list only shrinks. */
const ALLOWED: Record<string, string> = {
  'components/FileEditor.tsx': "the editor's own Save button turns into Saved",
  'pages/admin/OtodockTab.tsx': "the license key's result message, Saved. when the server words none",
}

const RETIRED = new Set(['SavedBadge', 'SavedIndicator'])
const SAVED_WORDS = new Set(['Saved', 'Saved.'])
const SAVED_SETTER = /^set\w*Saved\w*$/

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name)
    if (e.isDirectory()) {
      if (e.name !== 'tests') sourceFiles(p, out)
    } else if (/\.(ts|tsx)$/.test(e.name) && !/\.test\./.test(e.name) && !e.name.endsWith('.d.ts')) {
      out.push(p)
    }
  }
  return out
}

/** Every hand-made confirmation in one file, as `line: what`. */
function findings(rel: string, text: string): string[] {
  const sf = ts.createSourceFile(rel, text, ts.ScriptTarget.Latest, true, rel.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const out: string[] = []
  const at = (n: ts.Node) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1
  let timer = false
  const setters: ts.Node[] = []
  const visit = (n: ts.Node) => {
    if (ts.isJsxText(n) && SAVED_WORDS.has(n.text.trim())) out.push(`${at(n)}: the text ${n.text.trim()}`)
    if ((ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) && SAVED_WORDS.has(n.text)) {
      out.push(`${at(n)}: the string ${n.text}`)
    }
    if (ts.isImportSpecifier(n) && RETIRED.has(n.name.text)) out.push(`${at(n)}: an import of ${n.name.text}`)
    if (ts.isCallExpression(n) && ts.isIdentifier(n.expression)) {
      if (n.expression.text === 'setTimeout') timer = true
      if (SAVED_SETTER.test(n.expression.text)) setters.push(n)
    }
    if (ts.isCallExpression(n) && ts.isPropertyAccessExpression(n.expression) && n.expression.name.text === 'setTimeout') timer = true
    ts.forEachChild(n, visit)
  }
  visit(sf)
  if (timer) for (const n of setters) out.push(`${at(n)}: a saved flag cleared by a timer`)
  return out
}

const files = sourceFiles(SRC).map((f) => path.relative(SRC, f).split(path.sep).join('/'))
const found = new Map(files.map((rel) => [rel, findings(rel, readFileSync(path.join(SRC, rel), 'utf8'))]))

describe('one save indication', () => {
  // A floor that proves the scan reads the sources.
  it('reads the sources, the two files the indication lives in among them', () => {
    expect(files.length).toBeGreaterThan(500)
    for (const home of HOME) expect(files).toContain(home)
  })

  it('finds each hand-made shape, and passes the shared one', () => {
    const sample = [
      "import { SavedBadge } from '../ui/SettingsControls'",
      'const a = <span>Saved</span>',
      "const b = ok ? 'Saved.' : 'Save'",
      'const [s, setConfigSaved] = useState(false)',
      'const f = () => { setConfigSaved(true); setTimeout(() => setConfigSaved(false), 2000) }',
      "const c = <SaveState state={stateOf('alerts')} />",
      "const d = busy ? 'Saving…' : 'Save'",
      'const e = <p>Saved. Customers see the new hours.</p>',
    ].join('\n')
    expect(findings('sample.tsx', sample)).toEqual([
      '1: an import of SavedBadge',
      '2: the text Saved',
      '3: the string Saved.',
      '5: a saved flag cleared by a timer',
      '5: a saved flag cleared by a timer',
    ])
  })

  it('has no hand-made confirmation outside the shared one', () => {
    const offenders = [...found]
      .filter(([rel, list]) => list.length > 0 && !HOME.includes(rel) && !(rel in ALLOWED))
      .map(([rel, list]) => `${rel} ${list.join(', ')}`)
    expect(offenders).toEqual([])
  })

  it('keeps every allowed file on the list for its reason', () => {
    const stale = Object.keys(ALLOWED).filter((rel) => (found.get(rel) ?? []).length === 0)
    expect(stale).toEqual([])
  })
})
