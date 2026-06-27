import { describe, it, expect } from 'vitest'
import { createPtyBrandFilter } from '@/lib/ptyBrandColors'

const enc = new TextEncoder()
const dec = new TextDecoder()
const push = (f: ReturnType<typeof createPtyBrandFilter>, s: string) => dec.decode(f.push(enc.encode(s)))

describe('createPtyBrandFilter', () => {
  it('remaps the light diff-row background to the brand tint', () => {
    const f = createPtyBrandFilter('light')
    expect(push(f, '\x1b[48;2;105;219;124m+added\x1b[0m')).toBe('\x1b[48;2;165;214;167m+added\x1b[0m')
  })

  it('remaps foreground (38;2) word-diff colors too', () => {
    const f = createPtyBrandFilter('light')
    expect(push(f, '\x1b[38;2;209;69;75mword\x1b[m')).toBe('\x1b[38;2;218;53;54mword\x1b[m')
  })

  it('remaps inside chained SGR params', () => {
    const f = createPtyBrandFilter('light')
    expect(push(f, '\x1b[0;48;2;255;168;180;1m-x')).toBe('\x1b[0;48;2;242;175;175;1m-x')
  })

  it('uses the dark table in dark mode', () => {
    const f = createPtyBrandFilter('dark')
    expect(push(f, '\x1b[48;2;34;92;43m+')).toBe('\x1b[48;2;32;88;48m+')
    // A light-theme triple stays untouched in dark mode.
    expect(push(f, '\x1b[48;2;105;219;124m+')).toBe('\x1b[48;2;105;219;124m+')
  })

  it('remaps a sequence split across frames without losing bytes', () => {
    const f = createPtyBrandFilter('light')
    const a = push(f, 'pre\x1b[48;2;105;2')
    const b = push(f, '19;124mX')
    expect(a + b).toBe('pre\x1b[48;2;165;214;167mX')
  })

  it('holds a frame that is entirely a partial sequence', () => {
    const f = createPtyBrandFilter('light')
    expect(push(f, '\x1b[48;2;105')).toBe('')
    expect(push(f, ';219;124m')).toBe('\x1b[48;2;165;214;167m')
  })

  it('reset() drops a stale carried tail', () => {
    const f = createPtyBrandFilter('light')
    push(f, '\x1b[48;2;105')
    f.reset()
    expect(push(f, 'fresh')).toBe('fresh')
  })

  it('passes unknown colors, text, and multi-byte UTF-8 through byte-exact', () => {
    const f = createPtyBrandFilter('light')
    const s = '\x1b[48;2;1;2;3mκείμενο 日本語\x1b[0m plain'
    expect(push(f, s)).toBe(s)
  })

  it('brand-tints the user-message row background (bg only)', () => {
    const light = createPtyBrandFilter('light')
    expect(push(light, '\x1b[48;2;240;240;240m> hi\x1b[0m')).toBe('\x1b[48;2;232;244;253m> hi\x1b[0m')
    // The same gray as a FOREGROUND stays untouched — it is ordinary text.
    expect(push(light, '\x1b[38;2;240;240;240mtext')).toBe('\x1b[38;2;240;240;240mtext')
    const dark = createPtyBrandFilter('dark')
    expect(push(dark, '\x1b[48;2;55;55;55m> hi')).toBe('\x1b[48;2;30;42;66m> hi')
    expect(push(dark, '\x1b[38;2;55;55;55mtext')).toBe('\x1b[38;2;55;55;55mtext')
  })
})
