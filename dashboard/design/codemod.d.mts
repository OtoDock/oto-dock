// Types for the colour codemod (design/codemod.mjs), for its tests.
export const RECIPES: [string, string][]
export const RENAMES: Record<string, string>
export function splitClass(word: string): [string, string, string]
export function semanticFor(utility: string): string | null
export function rewriteClasses(text: string): [string, number, string[]]
export function rewriteSource(source: string, fileName?: string): [string, number, string[]]
