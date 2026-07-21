/**
 * Brand-tints the Claude TUI's hardcoded diff colors inside the PTY stream.
 *
 * The file-edit view paints added/removed rows with truecolor SGR values baked
 * into the CLI's theme — xterm's palette can't touch truecolor, and the CLI's
 * `-ansi` theme variants share ANSI slots between text and diff backgrounds
 * (success text and the added-row fill are both `ansi:green`), so neither lever
 * can brand the diff. Rewriting the six known rgb triples per mode in the byte
 * stream is the only seam. Triples are from Claude Code 2.1.201, re-verified byte-identical on 2.1.215 (the
 * VERSIONS.md pin) — a CLI bump that changes them degrades gracefully to the
 * stock colors. Dashboard-side only: real terminals (otodock-CLI) keep stock.
 */

// stock `R;G;B` → brand `R;G;B`, applied to fg (38;2;) and bg (48;2;) alike.
// Brand ramps: green from --color-p-success #4CAF50, red from
// --color-p-accent-red #da3536, at row / dimmed-row / word-highlight strengths.
const LIGHT_MAP: Record<string, string> = {
  '105;219;124': '165;214;167', // diffAdded row
  '199;225;203': '220;237;222', // diffAddedDimmed row
  '47;157;68': '67;160;71', // diffAddedWord
  '255;168;180': '242;175;175', // diffRemoved row
  '253;210;216': '249;220;220', // diffRemovedDimmed row
  '209;69;75': '218;53;54', // diffRemovedWord
}
const DARK_MAP: Record<string, string> = {
  '34;92;43': '32;88;48', // diffAdded row
  '71;88;74': '44;64;50', // diffAddedDimmed row
  '56;166;96': '76;175;80', // diffAddedWord
  '122;41;54': '112;40;44', // diffRemoved row
  '105;72;77': '74;44;46', // diffRemovedDimmed row
  '179;89;107': '218;83;84', // diffRemovedWord
}
// BACKGROUND-only remaps (48;2; — never 38;2;): the user-message row fill is a
// neutral gray in both CLI themes; brand-tint it blue (--brand-surface ramp).
// fg is excluded because these grays also appear as ordinary text colors.
const LIGHT_BG_MAP: Record<string, string> = {
  '240;240;240': '232;244;253', // userMessageBackground → brand-surface light
}
const DARK_BG_MAP: Record<string, string> = {
  '55;55;55': '30;42;66', // userMessageBackground → brand-surface dark
}

function buildRegex(map: Record<string, string>, bgOnly = false): RegExp {
  return new RegExp(`(${bgOnly ? '4' : '[34]'}8;2;)(${Object.keys(map).join('|')})(?=[;m])`, 'g')
}

// Byte-exact latin1 codec (TextDecoder's 'latin1' is really windows-1252 and
// remaps 0x80–0x9F, which would corrupt UTF-8 continuation bytes).
function bytesToLatin1(b: Uint8Array): string {
  let s = ''
  for (let i = 0; i < b.length; i += 0x8000) {
    s += String.fromCharCode(...b.subarray(i, Math.min(i + 0x8000, b.length)))
  }
  return s
}

function latin1ToBytes(s: string): Uint8Array {
  const out = new Uint8Array(s.length)
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i)
  return out
}

// A CSI can split across pty_output frames; hold back an incomplete trailing
// sequence so its triple still matches when the rest arrives. Bounded — a
// longer run isn't an SGR we remap. Holding bytes never changes rendering:
// xterm's parser can't paint a half sequence either.
const TRAILING_PARTIAL = /\x1b(?:\[[0-9;]{0,24})?$/

export interface PtyBrandFilter {
  push(bytes: Uint8Array): Uint8Array
  /** Drop carried state (call on a reset replay — the tail belongs to the old stream). */
  reset(): void
}

export function createPtyBrandFilter(mode: 'light' | 'dark'): PtyBrandFilter {
  const map = mode === 'dark' ? DARK_MAP : LIGHT_MAP
  const bgMap = mode === 'dark' ? DARK_BG_MAP : LIGHT_BG_MAP
  const re = buildRegex(map)
  const bgRe = buildRegex(bgMap, true)
  let tail = ''
  return {
    push(bytes: Uint8Array): Uint8Array {
      let s = tail + bytesToLatin1(bytes)
      tail = ''
      const partial = TRAILING_PARTIAL.exec(s)
      if (partial && partial[0].length < s.length) {
        tail = partial[0]
        s = s.slice(0, partial.index)
      } else if (partial) {
        // Whole buffer is one partial sequence — hold it all.
        tail = s
        return new Uint8Array(0)
      }
      s = s.replace(re, (_m, prefix: string, triple: string) => prefix + map[triple])
      s = s.replace(bgRe, (_m, prefix: string, triple: string) => prefix + bgMap[triple])
      return latin1ToBytes(s)
    },
    reset() {
      tail = ''
    },
  }
}
