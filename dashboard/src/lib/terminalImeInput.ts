// A phone keyboard's edit, sent as the edit it is.
//
// A keyboard that delivers a key as keyCode 229 outside a composition (a
// phone keyboard's digits, punctuation, Space and Backspace, a keyboard
// that does not compose, a desktop keyboard with the OS's text suggestions
// on) edits xterm's hidden textarea itself. xterm 5.5 then diffs that
// textarea on a 0 ms timer (CompositionHelper._handleAnyTextareaChanges)
// and gets it wrong whenever the keyboard rewrote text it had already
// committed: the whole value is sent again when the length is unchanged or
// the old text is not a prefix of the new, and a deletion of several
// characters is sent as one DEL. The textarea holds everything typed since
// the last Enter, so an autocorrect on Space or a word re-composed after a
// Backspace showed the whole sentence twice in the TUI.
//
// The textarea itself is left alone: a value written from script restarts a
// phone keyboard's input (its symbol layer, its context). Instead the data
// xterm sends for such a key is recognized (it equals xterm's own diff of
// the value recorded at the keydown) and replaced by the real edit: one DEL
// per character removed after the common prefix, then what was inserted.

const DEL = '\x7f'
const IME_KEY_CODE = 229

/** What xterm 5.5 sends for a keyCode-229 key outside a composition, given
 * the textarea before and after it. */
export function xtermTextareaDiff(before: string, after: string): string {
  if (after.length > before.length) return after.replace(before, '')
  if (after.length < before.length) return DEL
  return after !== before ? after : ''
}

// The TUIs delete one grapheme per Backspace (a flag, a skin-toned emoji, a
// letter with its combining accent), so the edit counts graphemes; a browser
// without the segmenter counts code points.
const GRAPHEMES = typeof Intl.Segmenter === 'function'
  ? new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  : null
const graphemes = (s: string): string[] =>
  GRAPHEMES ? Array.from(GRAPHEMES.segment(s), (g) => g.segment) : Array.from(s)

/** The bytes that take a terminal line holding `from` to `to` when the
 * typing happens at its end: a DEL per grapheme after the common prefix of
 * graphemes, then the rest of `to`. */
export function textareaEdit(from: string, to: string): string {
  const a = graphemes(from)
  const b = graphemes(to)
  let p = 0
  while (p < a.length && p < b.length && a[p] === b[p]) p++
  return DEL.repeat(a.length - p) + b.slice(p).join('')
}

export interface ImeEditRewriter {
  /** One `onData` chunk: xterm's send for a keyCode-229 key becomes the
   * real edit (possibly empty); anything else comes back unchanged. */
  rewrite(data: string): string
  dispose(): void
}

type Pending = { base: string; seq: number }

/**
 * Watch `textarea` for keyCode-229 keys. The keydown listener sits on
 * `root` (an ancestor) in the capture phase, so it reads the value before
 * xterm's own listener and xterm's single custom key handler (the
 * dashboard's copy and paste) stays untouched.
 */
export function attachImeEditRewriter(root: HTMLElement, textarea: HTMLTextAreaElement): ImeEditRewriter {
  let composing = false
  let pending: Pending[] = []
  // Edits sent so far, and the value the latest one brought the line to: a
  // second key recorded before the first was sent compares against that,
  // not against the value it saw (xterm's own timer repeats the first
  // key's characters then).
  let sent = 0
  let lineValue = ''
  const timers = new Set<ReturnType<typeof setTimeout>>()
  const later = (fn: () => void) => {
    const t = setTimeout(() => { timers.delete(t); fn() }, 0)
    timers.add(t)
  }

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.keyCode !== IME_KEY_CODE || composing || e.target !== textarea) return
    const rec: Pending = { base: textarea.value, seq: sent }
    pending.push(rec)
    // Dropped once xterm's 0 ms timer for this key has run: equal-delay
    // timers run in the order they were set, and the inner one is set from
    // a timer that ran before xterm's.
    later(() => later(() => { pending = pending.filter((r) => r !== rec) }))
  }
  const onCompositionStart = () => { composing = true }
  const onCompositionEnd = () => { composing = false }
  root.addEventListener('keydown', onKeyDown, true)
  textarea.addEventListener('compositionstart', onCompositionStart)
  textarea.addEventListener('compositionend', onCompositionEnd)

  return {
    rewrite(data: string): string {
      if (pending.length === 0) return data
      const value = textarea.value
      const i = pending.findIndex((r) => xtermTextareaDiff(r.base, value) === data)
      if (i < 0) return data
      const rec = pending[i]
      pending = pending.slice(i + 1)
      const base = rec.seq < sent ? lineValue : rec.base
      sent += 1
      lineValue = value
      return textareaEdit(base, value)
    },
    dispose() {
      root.removeEventListener('keydown', onKeyDown, true)
      textarea.removeEventListener('compositionstart', onCompositionStart)
      textarea.removeEventListener('compositionend', onCompositionEnd)
      for (const t of timers) clearTimeout(t)
      timers.clear()
      pending = []
    },
  }
}
