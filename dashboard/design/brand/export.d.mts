// Types for the brand export (design/brand/export.mjs), for the tests that
// import it.
export interface Image {
  width: number
  height: number
  rgba: Buffer
}
export interface OutputRow {
  path: string
  svg?: () => string
  opaque?: boolean
  ico?: () => { size: number; svg: string }[]
  text?: () => string
}
export const BRAND: string
export const DASHBOARD: string
export const REPO: string
export const MARK_BLUE: string
export const MARK_DEEP: string
export const WHITE: string
export const HERO_ANGLE: number
export const HERO_STOPS: [number, string][]
export const MARK_STATES: string[]
export const LOCKUP_STATES: string[]
export const TILE_FRAME: number
export const LOCKUP_RATIO: number
export function readMaster(name?: string): string
export function stateGroups(text: string): { id: string; shown: boolean }[]
export function markSvg(state?: string, opts?: { color?: string }): string
export function lockupSvg(state?: string, opts?: { color?: string }): string
export function pathData(id: string, text?: string): string
export function inlineMark(opts?: { state?: string; size?: number; className?: string; title?: string }): string
export function place(svg: string, x: number, y: number, w: number, h: number): string
export function fieldGradient(id: string, y1: number, y2: number): string
export function heroGradient(id: string, w: number, h: number): string
export function markOnClear(size: number, state?: string, color?: string): string
export function tileSvg(size: number, opts?: { box?: number; shape?: string; radius?: number; state?: string }): string
export function heroSvg(w: number, h: number, opts: { lockupWidth: number; left?: number }): string
export function render(svg: string): Image
export function encodePng(image: Image, opts?: { opaque?: boolean }): Buffer
export function decodePng(buf: Buffer): Image & { opaque: boolean }
export function encodeIco(frames: { width: number; height: number; png: Buffer }[]): Buffer
export function decodeIco(buf: Buffer): { width: number; height: number; png: Buffer }[]
export const PUBLIC_OUTPUTS: OutputRow[]
export function build(row: OutputRow): Buffer
export function compare(row: OutputRow, path: string, opts?: { tolerance?: number }): string | null
export function writeOutputs(rows: OutputRow[], base?: string): string[]
export function checkOutputs(rows: OutputRow[], base?: string, opts?: { tolerance?: number }): {
  stale: [string, string][]
  skipped: string[]
}
export function isEntry(url: string): boolean
