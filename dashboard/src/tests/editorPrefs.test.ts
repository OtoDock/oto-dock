import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { EDITOR_DARK_PAGE_KEY, EDITOR_DARK_THEME_KEY, seedEditorPrefs } from '@/components/chat/media/editorPrefs'

// ─── The editor's settings the dashboard writes before its frame loads. ─────

beforeEach(() => localStorage.clear())
afterEach(() => vi.restoreAllMocks())

describe('seedEditorPrefs', () => {
  it('writes the theme at every load and a white page when none is stored', () => {
    seedEditorPrefs('/collabora/browser/dist/cool.html?WOPISrc=x', true)
    expect(localStorage.getItem(EDITOR_DARK_THEME_KEY)).toBe('true')
    expect(localStorage.getItem(EDITOR_DARK_PAGE_KEY)).toBe('false')
    seedEditorPrefs('/collabora/browser/dist/cool.html?WOPISrc=x', false)
    expect(localStorage.getItem(EDITOR_DARK_THEME_KEY)).toBe('false')
  })

  it('never replaces a page the person chose', () => {
    localStorage.setItem(EDITOR_DARK_PAGE_KEY, 'true')
    seedEditorPrefs(`${window.location.origin}/collabora/cool.html`, true)
    expect(localStorage.getItem(EDITOR_DARK_PAGE_KEY)).toBe('true')
  })

  it('leaves an editor on another address alone', () => {
    seedEditorPrefs('https://collabora.example.com/browser/dist/cool.html', true)
    expect(localStorage.getItem(EDITOR_DARK_THEME_KEY)).toBeNull()
    expect(localStorage.getItem(EDITOR_DARK_PAGE_KEY)).toBeNull()
  })

  it('a storage that refuses is not an error', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('off') })
    expect(() => seedEditorPrefs('/collabora/cool.html', true)).not.toThrow()
  })

  it('an address that does not parse is not an error', () => {
    expect(() => seedEditorPrefs('http://[bad', true)).not.toThrow()
    expect(localStorage.getItem(EDITOR_DARK_THEME_KEY)).toBeNull()
  })
})
