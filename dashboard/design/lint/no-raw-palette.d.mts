// Types for the colour lint (design/lint/no-raw-palette.mjs), for its tests.
import type { Rule } from 'eslint'
export function bareUtility(word: string): string
export function classProblem(word: string): 'palette' | 'whiteBlack' | 'arbitrary' | null
export function hexColours(text: string): string[]
export function stringProblems(text: string): [string, string][]
declare const rule: Rule.RuleModule
export default rule
