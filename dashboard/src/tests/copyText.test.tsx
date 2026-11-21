// The one door to the clipboard (`lib/copyText.ts`): a plain-HTTP page has no
// `navigator.clipboard`, so the copy falls back, says so when it failed, and
// no other file touches the clipboard API.
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyOrSay, copyText, terminalClipboard } from '@/lib/copyText'
import { onAppToast } from '@/lib/appToasts'
import { pushModalLayer } from '@/lib/ui/modalLayers'

const SRC = resolve(__dirname, '..')

// Every way a file could reach the clipboard past the helper: the API by
// name or by bracket, the copy command, and the terminal's add-on left on
// its own provider.
const DIRECT = /clipboard\s*\??\.\s*write(Text)?\b|\[\s*['"`](clipboard|writeText)['"`]\s*\]|execCommand\(\s*['"`]copy['"`]|new ClipboardAddon\(\s*\)/

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return name === 'tests' ? [] : sources(path)
    return /\.tsx?$/.test(name) ? [path] : []
  })
}

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value })
}

function setExecCommand(fn: unknown) {
  Object.defineProperty(document, 'execCommand', { configurable: true, value: fn })
}

afterEach(() => {
  setClipboard(undefined)
  setExecCommand(undefined)
})

describe('copyText', () => {
  it('uses the clipboard where the browser offers it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    setClipboard({ writeText })
    expect(await copyText('a')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('a')
  })

  it('falls back to the copy command on a plain-HTTP page, in the open dialog', async () => {
    setClipboard(undefined)
    let parent: string | undefined
    setExecCommand(vi.fn(() => {
      parent = document.querySelector('textarea')?.parentElement?.getAttribute('role') ?? 'body'
      return true
    }))
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    const button = document.createElement('button')
    dialog.appendChild(button)
    document.body.appendChild(dialog)
    button.focus()
    expect(await copyText('a')).toBe(true)
    expect(parent).toBe('dialog')
    expect(document.querySelector('textarea')).toBeNull()
    expect(document.activeElement).toBe(button)
    dialog.remove()
  })

  it('falls back into the open dialog when the focus is on the page under it', async () => {
    setClipboard(undefined)
    let parent: Element | null | undefined
    setExecCommand(vi.fn(() => {
      parent = document.querySelector('textarea')?.parentElement
      return true
    }))
    // A native modal dialog makes the page under it inert: a textarea there
    // cannot be selected, so the copy must happen inside the dialog.
    const dialog = document.createElement('dialog')
    document.body.appendChild(dialog)
    const pop = pushModalLayer({ id: 'copy-test', el: dialog, portalRoot: dialog, onEscape: () => {} })
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(await copyText('a')).toBe(true)
    expect(parent).toBe(dialog)
    pop()
    dialog.remove()
  })

  it('falls back when the browser refuses, and answers false when nothing copied', async () => {
    setClipboard({ writeText: vi.fn().mockRejectedValue(new Error('denied')) })
    setExecCommand(vi.fn(() => false))
    expect(await copyText('a')).toBe(false)
  })
})

describe('copyOrSay', () => {
  it('says so when it could not copy, and stays quiet when it did', async () => {
    const seen: string[] = []
    const off = onAppToast((t) => { seen.push(t.title) })
    setClipboard(undefined)
    setExecCommand(vi.fn(() => false))
    expect(await copyOrSay('a')).toBe(false)
    expect(seen).toEqual(['Nothing was copied'])
    setExecCommand(vi.fn(() => true))
    expect(await copyOrSay('a')).toBe(true)
    expect(seen).toHaveLength(1)
    off()
  })
})

describe('the terminal clipboard', () => {
  it('writes through the same door, with its fallback and its notice', async () => {
    const seen: string[] = []
    const off = onAppToast((t) => { seen.push(t.title) })
    setClipboard(undefined)
    const copy = vi.fn(() => true)
    setExecCommand(copy)
    await terminalClipboard.writeText('c', 'from the terminal')
    expect(copy).toHaveBeenCalledWith('copy')
    setExecCommand(vi.fn(() => false))
    await terminalClipboard.writeText('c', 'again')
    expect(seen).toEqual(['Nothing was copied'])
    off()
  })
})

describe('the clipboard surface', () => {
  it('the guard sees every shape of a direct copy', () => {
    for (const line of [
      'navigator.clipboard.writeText(text)', 'navigator.clipboard?.writeText(text)',
      "navigator['clipboard'].writeText(text)", 'navigator.clipboard["writeText"](text)',
      "document.execCommand('copy')", 'document.execCommand("copy")',
      'navigator.clipboard.write([item])', 'term.loadAddon(new ClipboardAddon())',
    ]) expect(DIRECT.test(line), line).toBe(true)
    for (const line of [
      'actions.clipboard?.mode', 'new ClipboardAddon(terminalClipboard)', 'await copyOrSay(text)',
    ]) expect(DIRECT.test(line), line).toBe(false)
  })

  it('no file outside the helper touches the clipboard API', () => {
    const offenders = sources(SRC)
      .filter((path) => !path.endsWith(join('lib', 'copyText.ts')))
      .filter((path) => DIRECT.test(readFileSync(path, 'utf8')))
      .map((path) => relative(SRC, path))
    expect(offenders).toEqual([])
  })
})
