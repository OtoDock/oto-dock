/**
 * The interactive terminal's duplicated text: a keyboard
 * that delivers keys as keyCode 229 edits xterm's hidden textarea itself,
 * and xterm 5.5 diffs that textarea, sending the whole sentence again when
 * the keyboard rewrites committed text (an autocorrect on Space, a word
 * re-composed after a Backspace). Driven against the REAL xterm in jsdom:
 * the bytes a pretend TUI line receives, with and without the rewrite.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Terminal } from '@xterm/xterm'
import { attachImeEditRewriter, textareaEdit, xtermTextareaDiff } from '@/lib/terminalImeInput'

const DEL = '\x7f'

/** A TUI's input line: DEL takes the last character, the rest appends. */
class Line {
  text = ''
  apply(d: string) {
    for (const ch of Array.from(d)) {
      if (ch === DEL) this.text = Array.from(this.text).slice(0, -1).join('')
      else this.text += ch
    }
  }
}

function setup({ rewrite }: { rewrite: boolean }) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const term = new Terminal({})
  term.open(el)
  const ta = term.textarea as HTMLTextAreaElement
  const line = new Line()
  const rewriter = rewrite ? attachImeEditRewriter(term.element as HTMLElement, ta) : null
  const sent: string[] = []
  term.onData((d) => {
    const out = rewriter ? rewriter.rewrite(d) : d
    sent.push(out)
    line.apply(out)
  })
  ta.focus()

  const key229 = (type: 'keydown' | 'keyup') =>
    ta.dispatchEvent(new KeyboardEvent(type, { keyCode: 229, key: 'Unidentified', bubbles: true, cancelable: true }))
  const input = (inputType: string, data: string | null) =>
    ta.dispatchEvent(new InputEvent('input', { data, inputType, bubbles: true, composed: true }))
  /** One key the keyboard handles itself: it sets the box to `value`. */
  const imeKey = (value: string, inputType = 'insertText', data: string | null = null) => {
    key229('keydown')
    ta.value = value
    input(inputType, data)
    key229('keyup')
  }
  const typeEach = (text: string) => {
    for (const ch of text) {
      imeKey(ta.value + ch, 'insertText', ch)
      vi.runAllTimers()
    }
  }
  return { term, ta, line, rewriter, sent, imeKey, typeEach, key229 }
}

beforeEach(() => {
  vi.useFakeTimers()
  // jsdom has no canvas: the xterm renderer probes one at open.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
    dispatchEvent: () => false,
  }))
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('xterm 5.5 on a keyCode-229 keyboard (the cause)', () => {
  it('sends the whole sentence again after an autocorrect on Space', () => {
    const k = setup({ rewrite: false })
    k.typeEach('we fix teh')
    k.imeKey('we fix the ', 'insertReplacementText', 'the ')
    vi.runAllTimers()
    expect(k.line.text).toBe('we fix tehwe fix the ')
  })

  it('sends the whole sentence again after a same-length re-compose', () => {
    const k = setup({ rewrite: false })
    k.typeEach('we fix the bug')
    k.imeKey('we fix the bus', 'insertCompositionText', 'bus')
    vi.runAllTimers()
    expect(k.line.text).toBe('we fix the bugwe fix the bus')
  })
})

describe('the rewrite sends the edit the keyboard made', () => {
  it('an autocorrect on Space', () => {
    const k = setup({ rewrite: true })
    k.typeEach('we fix teh')
    k.imeKey('we fix the ', 'insertReplacementText', 'the ')
    vi.runAllTimers()
    expect(k.line.text).toBe('we fix the ')
    expect(k.ta.value).toBe('we fix the ')
  })

  it('a word re-composed after a Backspace', () => {
    const k = setup({ rewrite: true })
    k.typeEach('we fix the bug')
    k.imeKey('we fix the bu', 'deleteContentBackward')
    vi.runAllTimers()
    k.imeKey('we fix the bus', 'insertCompositionText', 'bus')
    vi.runAllTimers()
    expect(k.line.text).toBe('we fix the bus')
  })

  it('a same-length rewrite with no Backspace before it', () => {
    const k = setup({ rewrite: true })
    k.typeEach('we fix the bug')
    k.imeKey('we fix the bus', 'insertReplacementText', 'bus')
    vi.runAllTimers()
    expect(k.line.text).toBe('we fix the bus')
  })

  it('a word deleted at once (xterm sent a single DEL)', () => {
    const k = setup({ rewrite: true })
    k.typeEach('hello world')
    k.imeKey('hello ', 'deleteWordBackward')
    vi.runAllTimers()
    expect(k.line.text).toBe('hello ')
  })

  it('two keys before the first timer runs (xterm repeats the first)', () => {
    const bare = setup({ rewrite: false })
    bare.imeKey('a', 'insertText', 'a')
    bare.imeKey('ab', 'insertText', 'b')
    vi.runAllTimers()
    expect(bare.line.text).toBe('abb')
    document.body.innerHTML = ''

    const k = setup({ rewrite: true })
    k.imeKey('a', 'insertText', 'a')
    k.imeKey('ab', 'insertText', 'b')
    vi.runAllTimers()
    expect(k.line.text).toBe('ab')
    k.typeEach('c')
    expect(k.line.text).toBe('abc')
  })

  it('a composition between keyboard keys keeps its word once', () => {
    const k = setup({ rewrite: true })
    k.typeEach('we ')
    k.ta.dispatchEvent(new CompositionEvent('compositionstart', { data: '' }))
    k.ta.value = 'we fix'
    k.ta.dispatchEvent(new CompositionEvent('compositionupdate', { data: 'fix' }))
    k.ta.dispatchEvent(new CompositionEvent('compositionend', { data: 'fix' }))
    vi.runAllTimers()
    k.typeEach(' it')
    expect(k.line.text).toBe('we fix it')
  })

  it('leaves desktop keys and pastes untouched', () => {
    const k = setup({ rewrite: true })
    k.ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', keyCode: 65, bubbles: true, cancelable: true }))
    vi.runAllTimers()
    k.term.paste('pasted')
    vi.runAllTimers()
    expect(k.line.text).toBe('apasted')
    expect(k.ta.value).toBe('')
  })

  it('keeps the dashboard custom key handler the only one', () => {
    const k = setup({ rewrite: true })
    const seen: string[] = []
    k.term.attachCustomKeyEventHandler((e) => {
      if (e.type === 'keydown' && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') {
        seen.push('paste')
        return false
      }
      return true
    })
    k.ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'v', keyCode: 86, ctrlKey: true, bubbles: true, cancelable: true }))
    vi.runAllTimers()
    expect(seen).toEqual(['paste'])
    expect(k.line.text).toBe('')
  })

  it('stops reading the box once disposed', () => {
    const k = setup({ rewrite: true })
    k.rewriter?.dispose()
    k.typeEach('ab')
    k.imeKey('ax', 'insertReplacementText', 'x')
    vi.runAllTimers()
    // Back to xterm's own diff: the same-length rewrite resends the value.
    expect(k.line.text).toBe('abax')
  })
})

describe('the two helpers', () => {
  it('replicate xterm 5.5 and compute the edit', () => {
    expect(xtermTextareaDiff('ab', 'abc')).toBe('c')
    expect(xtermTextareaDiff('teh', 'the ')).toBe('the ')
    expect(xtermTextareaDiff('abc', 'a')).toBe(DEL)
    expect(xtermTextareaDiff('bug', 'bus')).toBe('bus')
    expect(xtermTextareaDiff('same', 'same')).toBe('')
    expect(textareaEdit('we fix teh', 'we fix the ')).toBe(`${DEL}${DEL}he `)
    expect(textareaEdit('abc', 'abc')).toBe('')
    expect(textareaEdit('', 'abc')).toBe('abc')
    expect(textareaEdit('a😀', 'a😃')).toBe(`${DEL}😃`)
    // A grapheme of several code points is one Backspace in the TUI.
    expect(textareaEdit('ok 👍🏽', 'ok ')).toBe(DEL)
    expect(textareaEdit('to 🇬🇷', 'to ')).toBe(DEL)
    expect(textareaEdit('👍🏽', '👍🏾')).toBe(`${DEL}👍🏾`)
    expect(textareaEdit('café', 'cafe')).toBe(`${DEL}e`)
  })
})

describe('a Backspace over a grapheme of several code points', () => {
  it('sends one DEL for a skin-toned emoji and for a flag', () => {
    for (const glyph of ['👍🏽', '🇬🇷']) {
      const k = setup({ rewrite: true })
      k.imeKey(`ok ${glyph}`, 'insertText', `ok ${glyph}`)
      vi.runAllTimers()
      const before = k.line.text
      k.imeKey('ok ', 'deleteContentBackward')
      vi.runAllTimers()
      expect(k.sent[k.sent.length - 1]).toBe(DEL)
      expect(before.endsWith(glyph)).toBe(true)
      document.body.innerHTML = ''
    }
  })

  it('a skin-tone swap sends a DEL and the whole new emoji', () => {
    const k = setup({ rewrite: true })
    k.imeKey('👍🏽', 'insertText', '👍🏽')
    vi.runAllTimers()
    k.imeKey('👍🏾', 'insertReplacementText', '👍🏾')
    vi.runAllTimers()
    expect(k.sent[k.sent.length - 1]).toBe(`${DEL}👍🏾`)
  })
})
