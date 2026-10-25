/**
 * The key rule of a multi-line box that sends (the chat composer and the plan
 * review's feedback box): plain Enter adds a line on every device; Shift, Ctrl
 * or Cmd+Enter sends on a fine-pointer device; a touch device sends only with
 * the box's button, so a phone keyboard's auto-capitalised Shift never sends.
 */

type EnterKeyEvent = Pick<
  KeyboardEvent,
  'key' | 'shiftKey' | 'ctrlKey' | 'metaKey' | 'altKey' | 'repeat' | 'isComposing' | 'keyCode'
>

/** `pass`: leave the event to the browser (a line, or the IME's own Enter).
 * `send`: prevent it and send. `newline`: prevent it and add the line
 * ourselves (a modifier still held from the paste). `swallow`: prevent it and
 * do nothing (an auto-repeated send key). */
export type EnterAction = 'pass' | 'send' | 'newline' | 'swallow'

/** An IME composition owns the key: Safari fires the Enter that commits it
 * after `compositionend`, with keyCode 229 and `isComposing` false. */
export function isComposingKey(e: Pick<KeyboardEvent, 'isComposing' | 'keyCode'>): boolean {
  return e.isComposing || e.keyCode === 229
}

export function enterAction(
  e: EnterKeyEvent,
  opts: { coarse: boolean; pasteHeld: boolean },
): EnterAction {
  if (e.key !== 'Enter' || isComposingKey(e)) return 'pass'
  // Alt rules out AltGr+Enter, which arrives as Ctrl+Alt.
  if (e.altKey || !(e.shiftKey || e.ctrlKey || e.metaKey)) return 'pass'
  if (opts.coarse) return 'pass'
  if (opts.pasteHeld) return 'newline'
  if (e.repeat) return 'swallow'
  return 'send'
}

/** The composer's placeholder with how to send: a desktop names the key, a
 * touch device keeps it short (the Send arrow is the way there). */
export function withSendHint(text: string, touchPrimary: boolean): string {
  return touchPrimary ? `${text}...` : `${text}, Shift+Enter to send`
}

/** Add a line at the caret. `insertText` keeps the browser's undo history and
 * reaches the box's onChange through its input event; where it is missing or
 * refused the value is set directly and handed to `onChange`. */
export function insertNewline(ta: HTMLTextAreaElement, onChange: (value: string) => void): void {
  let inserted: boolean
  try {
    inserted = typeof document.execCommand === 'function' && document.execCommand('insertText', false, '\n')
  } catch {
    inserted = false
  }
  if (inserted) return
  ta.setRangeText('\n', ta.selectionStart, ta.selectionEnd, 'end')
  onChange(ta.value)
}

const FILE_URI = /^file:\/\//i
const ABSOLUTE_PATH = /^(\/|~\/|[A-Za-z]:[\\/]|\\\\)/
const SINGLE_URL = /^https?:\/\/\S+$/i

function stem(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(0, dot) : name
}

/** A paste with files on the clipboard: attach the files, or let the browser
 * insert the text. Word, Excel and LibreOffice put the copied prose or cells
 * on the clipboard beside a rendered picture of them, so the text wins there;
 * a screenshot carries no text, and a copied file or image carries only its
 * name, its path, its `file://` URI or its address, so the files win. `rich`
 * is the clipboard's `text/html` and `text/rtf`: rich text that holds no
 * image is an office copy (one cell may hold just an address or a path). */
export function pasteAttachesFiles(text: string, files: ArrayLike<{ name: string }>,
                                   rich = ''): boolean {
  const t = text.normalize('NFC').replace(/\uFFFC/g, '').trim()
  if (!t) return true
  if (rich.trim() && !/<img\b/i.test(rich)) return false
  const lines = t.split(/\r?\n/).map((l) => l.trim()).filter(Boolean)
  if (lines.length === 1 && SINGLE_URL.test(lines[0])) return true
  const names = new Set<string>()
  for (const f of Array.from(files)) {
    const n = f.name.normalize('NFC').toLowerCase()
    names.add(n)
    names.add(stem(n))
  }
  return lines.every((line) => {
    if (FILE_URI.test(line)) {
      try {
        decodeURIComponent(line)
        return true
      } catch {
        return false
      }
    }
    return ABSOLUTE_PATH.test(line) || names.has(line.toLowerCase())
  })
}
