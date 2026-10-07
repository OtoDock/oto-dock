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
  let inserted = false
  try {
    inserted = typeof document.execCommand === 'function' && document.execCommand('insertText', false, '\n')
  } catch {
    inserted = false
  }
  if (inserted) return
  ta.setRangeText('\n', ta.selectionStart, ta.selectionEnd, 'end')
  onChange(ta.value)
}
