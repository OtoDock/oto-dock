// @vitest-environment node
// The colour codemod (design/codemod.mjs): the recipes it rewrites and what
// it leaves to a person.
import { describe, expect, it } from 'vitest'

import { rewriteClasses, rewriteSource, semanticFor } from '../../design/codemod.mjs'

const rewrite = (s: string) => rewriteClasses(s)[0]

describe('the colour codemod', () => {
  it('turns a light and dark pair into one semantic class', () => {
    expect(rewrite('text-amber-700 dark:text-amber-400 font-medium')).toBe('text-status-warning-fg font-medium')
    expect(rewrite('bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800'))
      .toBe('bg-status-danger-bg border border-status-danger-border')
    expect(rewrite('hover:bg-red-50 dark:hover:bg-red-900/20')).toBe('hover:bg-status-danger-bg')
    expect(rewrite('text-gray-500 dark:text-gray-400')).toBe('text-tertiary')
  })

  it('maps a single light class by its hue and slot', () => {
    expect(semanticFor('bg-green-500')).toBe('bg-status-success-solid')
    expect(semanticFor('bg-red-600')).toBe('bg-status-danger-fill')
    expect(semanticFor('text-purple-700')).toBe('text-accent-agent-fg')
    expect(semanticFor('border-gray-300')).toBe('border-default')
    expect(semanticFor('bg-blue-100')).toBeNull()
    expect(rewrite('bg-amber-500/15')).toBe('bg-status-warning-bg')
  })

  it('replaces the known recipes and renames the alias names', () => {
    expect(rewrite('bg-white dark:bg-p-surface rounded-xl')).toBe('bg-surface-card rounded-xl')
    expect(rewrite('bg-white/80 dark:bg-gray-900/80 backdrop-blur-xs border border-p-border-light'))
      .toBe('bg-glass-fill backdrop-blur-xs border border-glass-border')
    expect(rewrite('text-p-text-light hover:bg-p-surface-hover border-p-border-light/50'))
      .toBe('text-tertiary hover:bg-surface-hover border-subtle/50')
    expect(rewrite('bg-brand text-white hover:bg-brand-hover')).toBe('bg-action text-action-fg hover:bg-action-hover')
    expect(rewrite('bg-red-600 hover:bg-red-700 text-white'))
      .toBe('bg-status-danger-fill hover:bg-status-danger-fill-hover text-on-solid')
    expect(rewrite('fixed inset-0 bg-black/40')).toBe('fixed inset-0 bg-surface-scrim')
    expect(rewrite('hover:text-brand-hover underline')).toBe('hover:text-link-hover underline')
  })

  it('reports what it will not guess', () => {
    expect(rewriteClasses('dark:text-red-400 text-xs')[2]).toEqual(['dark:text-red-400'])
    expect(rewriteClasses('text-blue-700')[2]).toEqual(['text-blue-700'])
    expect(rewriteClasses('text-brand underline')[2]).toEqual(['text-brand'])
    expect(rewriteClasses('text-white')[2]).toEqual(['text-white'])
  })

  it('keeps the indentation of a multi-line class string', () => {
    expect(rewrite('a\n          bg-gray-100 dark:bg-gray-800\n          px-2')).toBe('a\n          bg-surface-muted\n          px-2')
  })

  it('rewrites strings only, in a whole file', () => {
    const src = "// text-amber-700 in a comment\nconst A = 'text-amber-700 dark:text-amber-400'\nconst B = `p-2 ${x ? 'bg-p-bg' : ''}`\n"
    const [out, changes] = rewriteSource(src, 'x.tsx')
    expect(out).toBe("// text-amber-700 in a comment\nconst A = 'text-status-warning-fg'\nconst B = `p-2 ${x ? 'bg-surface-page' : ''}`\n")
    expect(changes).toBe(2)
  })
})
