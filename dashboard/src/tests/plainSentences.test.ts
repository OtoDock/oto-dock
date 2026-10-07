import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// What a person reads in the apps and sharing screens is plain sentences:
// no semicolon in a string literal, a template's text or JSX text (the
// operator, 2026-10-05). Class names, inline styles and SVG paths are not
// prose; a line may opt out with a `plain-sentences: allow` comment above it.

const src = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const SCANNED = [
  ...readdirSync(path.join(src, 'components/apps')).map((f) => `components/apps/${f}`),
  ...readdirSync(path.join(src, 'components/sharing')).map((f) => `components/sharing/${f}`),
  'pages/admin/SharesPage.tsx',
  'pages/admin/SecurityTab.tsx',
  'pages/apps/AppPage.tsx',
  'api/apps.ts',
].filter((f) => /\.tsx?$/.test(f))
const NOT_PROSE_ATTRIBUTES = new Set(['className', 'style', 'd', 'points', 'viewBox'])
const ALLOW = 'plain-sentences: allow'

function semicolonProse(file: string, text = readFileSync(path.join(src, file), 'utf8')): string[] {
  const lines = text.split('\n')
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true,
    file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const found: string[] = []
  const check = (node: ts.Node, raw: string) => {
    const prose = raw.replace(/&[a-z0-9#]+;/gi, ' ')
    if (!prose.includes(';')) return
    const line = sf.getLineAndCharacterOfPosition(node.getStart()).line
    if (line > 0 && lines[line - 1].includes(ALLOW)) return
    found.push(`${file}:${line + 1}: ${prose.replace(/\s+/g, ' ').trim()}`)
  }
  const visit = (node: ts.Node) => {
    if (ts.isJsxAttribute(node) && NOT_PROSE_ATTRIBUTES.has(node.name.getText(sf))) return
    if (ts.isImportDeclaration(node)) return
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)
        || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
      check(node, node.text)
    } else if (ts.isJsxText(node)) {
      check(node, node.getText(sf))
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return found
}

describe('plain sentences in the apps and sharing screens', () => {
  it('scans the two folders and the admin page', () => {
    expect(SCANNED).toContain('components/apps/AppsOverlay.tsx')
    expect(SCANNED).toContain('components/sharing/SharePopover.tsx')
    expect(SCANNED).toContain('pages/admin/SharesPage.tsx')
  })

  it('finds a semicolon in a string, a template and JSX text, and nowhere else', () => {
    const sample = [
      "const a = 'one; two'",
      'const b = `three; ${a} four`',
      'const c = <p className="x; y" style={{}}>five;six&nbsp;seven</p>',
      'const t = <b title="eight; nine" aria-label="ten">x</b>',
      '// plain-sentences: allow',
      "const d = 'kept; on purpose'",
      "const e = 'no semicolon here &amp; there'",
    ].join('\n')
    expect(semicolonProse('sample.tsx', sample).map((f) => f.split(': ')[0])).toEqual([
      'sample.tsx:1', 'sample.tsx:2', 'sample.tsx:3', 'sample.tsx:4',
    ])
  })

  it('no text a person reads holds a semicolon', () => {
    expect(SCANNED.flatMap((f) => semicolonProse(f))).toEqual([])
  })
})
