// Types for the token generator (design/build.mjs), for the tests that
// import it.
export interface ContrastRow {
  fg: string
  bg: string
  min: number
  mode: 'light' | 'dark'
  ratio: number
  pass: boolean
  over?: string
}
export interface SemanticColour {
  path: string[]
  name: string
  light: string
  dark: string
  description?: string
}
export interface Token {
  path: string[]
  type: string
  value: unknown
}
export interface Model {
  tokens: Map<string, Token>
  semantic: SemanticColour[]
  aliases: { name: string; target: string }[]
  resolve: (name: string, mode: 'light' | 'dark') => unknown
}
export const DASHBOARD: string
export const TOKENS_PATH: string
export const MODES: ['light', 'dark']
export function loadTokens(path?: string): Record<string, unknown>
export function flatten(tree: Record<string, unknown>): Map<string, Token>
export function makeResolver(tokens: Map<string, unknown>): (name: string, mode: 'light' | 'dark') => unknown
export function model(tree?: Record<string, unknown>): Model
export function contrast(fg: string, bg: string): number
export function composite(fg: string, bg: string): string
export function cssColor(hex: string): string
export function parseHex(hex: string): { r: number; g: number; b: number; a: number }
/** The token groups the kit leaves out, each with its reason. */
export const KIT_EXCLUDED: [string, string][]
/** The kit's names with their values in one mode; `reads` collects every
 *  token path the kit resolves. */
export function kitVariables(m: Model, mode: 'light' | 'dark', reads?: Set<string> | null): [string, string][]
export function varName(path: string[]): string
export function themeKey(path: string[]): string
export function contrastPairs(): { fg: string; bg: string; min: number; over?: string }[]
export function measureContrast(m: Model): ContrastRow[]
export function buildOutputs(tree?: Record<string, unknown>): Map<string, string>
export function referencePage(m: Model): string
export function referenceCss(html: string, dashboardDir: string): Promise<string>
