// @vitest-environment node
// The colour lint (design/lint/no-raw-palette.mjs): what it reports and
// what it leaves alone, in every position a class string takes.
import { RuleTester } from 'eslint'
import tseslint from 'typescript-eslint'
import { describe, expect, it } from 'vitest'

import rule, { bareUtility, classProblem, hexColours } from '../../design/lint/no-raw-palette.mjs'

describe('class words', () => {
  it('strips variants, important marks and opacity', () => {
    expect(bareUtility('dark:hover:bg-amber-900/30')).toBe('bg-amber-900')
    expect(bareUtility('[&>svg]:text-red-500')).toBe('text-red-500')
    expect(bareUtility('data-[state=open]:!bg-white')).toBe('bg-white')
    expect(bareUtility('group-hover/row:text-p-text')).toBe('text-p-text')
    expect(bareUtility('bg-[#fff]/50')).toBe('bg-[#fff]')
  })

  it('names the kind of each problem', () => {
    expect(classProblem('text-amber-700')).toBe('palette')
    expect(classProblem('dark:bg-gray-800/50')).toBe('palette')
    expect(classProblem('border-l-red-500')).toBe('palette')
    expect(classProblem('ring-offset-blue-50')).toBe('palette')
    expect(classProblem('bg-white')).toBe('whiteBlack')
    expect(classProblem('text-white/80')).toBe('whiteBlack')
    expect(classProblem('bg-[#f4b206]/10')).toBe('arbitrary')
    expect(classProblem('shadow-[0_0_8px_#f4b206]')).toBe('arbitrary')
    expect(classProblem('text-[rgb(0,0,0)]')).toBe('arbitrary')
    expect(classProblem('text-[color:#333]')).toBe('arbitrary')
    expect(classProblem('inset-ring-red-500/20')).toBe('palette')
    expect(classProblem('supports-[display:grid]:bg-gray-50')).toBe('palette')
  })

  it('leaves the tokens and everything else alone', () => {
    for (const w of [
      'bg-surface-card', 'text-primary', 'border-subtle', 'bg-status-warning-bg', 'text-accent-agent-fg',
      'bg-brand', 'hover:bg-brand-hover', 'text-p-text-light', 'rounded-lg', 'text-sm', 'border-2',
      'shadow-elevation-3', 'ring-2', 'bg-[url(/x.png)]', 'shadow-[inset_3px_0_0_0_var(--color-brand-surface)]',
      'text-[10px]', 'bg-transparent', 'text-current', 'from-image', 'blue-500',
    ]) {
      expect(classProblem(w), w).toBeNull()
    }
  })

  it('finds hex colours but not issue numbers', () => {
    expect(hexColours('#146bb5')).toEqual(['#146bb5'])
    expect(hexColours('color:#333;background:#FAF9F9')).toEqual(['#333', '#FAF9F9'])
    expect(hexColours('linear-gradient(90deg,#fff,#000000cc)')).toEqual(['#fff', '#000000cc'])
    expect(hexColours('#333')).toEqual(['#333'])
    expect(hexColours('PR #123 merged')).toEqual([])
    expect(hexColours('see https://example.com/#abc')).toEqual([])
    expect(hexColours('#section-two')).toEqual([])
  })
})

const tester = new RuleTester({
  languageOptions: {
    parser: tseslint.parser,
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
})

// RuleTester opens its own describe and it blocks (vitest's globals).
describe('the rule', () => {
  tester.run('no-raw-palette', rule, {
    valid: [
      { code: 'const a = <div className="bg-surface-card text-primary border border-subtle" />' },
      { code: "const b = ok ? 'bg-status-warning-bg text-status-warning-fg' : 'bg-surface-muted'" },
      { code: "const MAP = { admin: 'bg-status-danger-bg text-status-danger-fg' }" },
      { code: 'const c = `rounded-card ${x ? "bg-action text-action-fg" : "text-secondary"}`' },
      { code: "const d = 'PR #123 merged'" },
      { code: "const e = { color: 'var(--color-surface-card)' }" },
    ],
    invalid: [
      {
        code: 'const a = <div className="bg-white dark:bg-gray-800" />',
        errors: [{ messageId: 'whiteBlack' }, { messageId: 'palette' }],
      },
      {
        code: "const b = ok ? 'text-amber-700 dark:text-amber-400' : ''",
        errors: [{ messageId: 'palette' }, { messageId: 'palette' }],
      },
      { code: "const MAP = { admin: 'bg-red-100 text-p-text' }", errors: [{ messageId: 'palette' }] },
      { code: 'const c = `px-2 ${x} hover:bg-blue-50`', errors: [{ messageId: 'palette' }] },
      { code: "const chipClass = 'bg-[#f4b206]/10'", errors: [{ messageId: 'arbitrary' }] },
      { code: "const style = { color: '#146bb5' }", errors: [{ messageId: 'hex' }] },
      { code: "const css = 'a{color:#333}'", errors: [{ messageId: 'hex' }] },
    ],
  })
})
