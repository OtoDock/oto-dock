// The one door to the clipboard. A page served over plain HTTP on a LAN
// address has no `navigator.clipboard`, so a direct call there copies nothing
// and says nothing. A test keeps every other file off the clipboard API.

import { emitAppToast } from './appToasts'
import { topModal } from './ui/modalLayers'

/** The clipboard where the browser offers it, else a selected textarea and
 * the copy command. True when the text was copied. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Refused (no focus, no permission): the fallback below.
  }
  const had = document.activeElement instanceof HTMLElement ? document.activeElement : null
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  // Inside the open dialog: a modal one makes the page under it inert, so a
  // textarea on the page cannot be selected, wherever the focus sits.
  ;(topModal()?.el ?? had?.closest('dialog, [role="dialog"]') ?? document.body).appendChild(ta)
  ta.select()
  let ok: boolean
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  ta.remove()
  had?.focus()
  return ok
}

/** `copyText`, and a press that could not copy says so. */
export async function copyOrSay(text: string): Promise<boolean> {
  const ok = await copyText(text)
  if (!ok) {
    emitAppToast({
      title: 'Nothing was copied',
      body: 'This browser refused the copy. Select the text and copy it by hand.',
      severity: 'warning',
    })
  }
  return ok
}

/** The terminal's clipboard (xterm's ClipboardAddon, the TUI's OSC 52 copy)
 * through the same door: its own provider writes to `navigator.clipboard`
 * alone, which a plain-HTTP page does not have. */
export const terminalClipboard = {
  readText: async (): Promise<string> => {
    try {
      return (await navigator.clipboard?.readText?.()) ?? ''
    } catch {
      return ''
    }
  },
  writeText: async (_selection: string, text: string): Promise<void> => {
    await copyOrSay(text)
  },
}
