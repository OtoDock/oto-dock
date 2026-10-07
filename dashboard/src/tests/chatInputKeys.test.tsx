import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, renderHook, screen } from '@testing-library/react'

// The composer's key rule: plain Enter adds a line; Shift, Ctrl or Cmd+Enter
// sends on a fine-pointer device; a touch device sends only with the button.
// An IME composition owns its Enter, a modifier still held from a paste turns
// the next send key into a line (for 3 s at most), and a running turn shows
// Send beside a square Stop.

vi.mock('@/hooks/useSpeechSession', () => ({
  useSpeechSession: () => ({ available: false, status: 'idle', start: vi.fn(), stop: vi.fn(), toggle: vi.fn() }),
}))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: null }) }))
vi.mock('@/components/chat/media/ImageLightbox', () => ({ default: () => null }))
vi.mock('@/components/chat/VoiceControl', () => ({ VoiceControl: () => null }))
vi.mock('@/components/chat/PresenceHalo', () => ({ PresenceHalo: () => null }))

import ChatInput from '@/components/chat/ChatInput'
import { enterAction, withSendHint } from '@/lib/composerKeys'
import { usePrimaryPointerCoarse } from '@/hooks/usePrimaryPointerCoarse'
import { PASTE_GUARD_MS } from '@/hooks/usePasteGuard'

function props(over: Record<string, unknown> = {}) {
  return {
    value: '  hello  ',
    onChange: vi.fn(),
    onSend: vi.fn(),
    pendingImages: [],
    onAddImages: vi.fn(),
    onRemoveImage: vi.fn(),
    pendingFiles: [],
    onAddFiles: vi.fn(),
    onRemoveFile: vi.fn(),
    draftKey: 'c1',
    ...over,
  }
}

function stubPointer(coarse: boolean) {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: coarse && q === '(pointer: coarse)',
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia
}

const box = () => screen.getByRole('textbox') as HTMLTextAreaElement
// fireEvent returns false when the handler prevented the default.
const press = (init: Record<string, unknown>) => !fireEvent.keyDown(box(), { key: 'Enter', ...init })
const textPaste = () => fireEvent.paste(box(), { clipboardData: { files: [], getData: () => 'pasted' } })

afterEach(() => {
  delete (window as { matchMedia?: unknown }).matchMedia
  vi.useRealTimers()
})

describe('ChatInput keys on a desktop', () => {
  it('plain Enter adds a line and never sends', () => {
    const p = props()
    render(<ChatInput {...p} />)
    expect(press({})).toBe(false)
    expect(p.onSend).not.toHaveBeenCalled()
  })

  it.each([
    ['Shift', { shiftKey: true }],
    ['Ctrl', { ctrlKey: true }],
    ['Cmd', { metaKey: true }],
  ])('%s+Enter sends the trimmed text once', (_name, mods) => {
    const p = props()
    render(<ChatInput {...p} />)
    expect(press(mods)).toBe(true)
    expect(p.onSend).toHaveBeenCalledTimes(1)
    expect(p.onSend).toHaveBeenCalledWith('hello')
  })

  it('AltGr+Enter (Ctrl+Alt) is left alone', () => {
    const p = props()
    render(<ChatInput {...p} />)
    expect(press({ ctrlKey: true, altKey: true })).toBe(false)
    expect(p.onSend).not.toHaveBeenCalled()
  })

  it('an auto-repeated send key is swallowed', () => {
    const p = props()
    render(<ChatInput {...p} />)
    expect(press({ shiftKey: true, repeat: true })).toBe(true)
    expect(p.onSend).not.toHaveBeenCalled()
  })

  it('an IME composition keeps its Enter: no send, no line of ours', () => {
    const p = props()
    render(<ChatInput {...p} />)
    expect(press({ shiftKey: true, isComposing: true })).toBe(false)
    expect(press({ ctrlKey: true, keyCode: 229 })).toBe(false)
    expect(press({ keyCode: 229 })).toBe(false)
    expect(p.onSend).not.toHaveBeenCalled()
    expect(p.onChange).not.toHaveBeenCalled()
  })

  it('a send key with nothing to send leaves no stray line', () => {
    const p = props({ value: '' })
    render(<ChatInput {...p} />)
    expect(press({ shiftKey: true })).toBe(true)
    expect(p.onSend).not.toHaveBeenCalled()
    expect(p.onChange).not.toHaveBeenCalled()
  })

  it('an upload in flight holds the send keys', () => {
    const p = props({ pendingFiles: [{ id: 'f', name: 'a.pdf', size: 1, uploading: true }] })
    render(<ChatInput {...p} />)
    expect(press({ shiftKey: true })).toBe(true)
    expect(p.onSend).not.toHaveBeenCalled()
  })

  it('Shift+Enter during a turn sends: the host queues it', () => {
    const p = props({ streaming: true, queueable: true, onAbort: vi.fn() })
    render(<ChatInput {...p} />)
    press({ shiftKey: true })
    expect(p.onSend).toHaveBeenCalledWith('hello')
  })

  it('ArrowUp in an empty composer still takes the queued messages back', () => {
    const onEditQueued = vi.fn()
    render(<ChatInput {...props({ value: '', queuedCount: 2, onEditQueued })} />)
    fireEvent.keyDown(box(), { key: 'ArrowUp' })
    expect(onEditQueued).toHaveBeenCalledTimes(1)
  })

  it('the Send button names its keys', () => {
    render(<ChatInput {...props()} />)
    const send = screen.getByRole('button', { name: 'Send' })
    expect(send).toHaveAttribute('title', 'Send (Shift+Enter)')
    expect(send).toHaveAttribute('aria-keyshortcuts', 'Shift+Enter Control+Enter Meta+Enter')
  })
})

describe('ChatInput keys on a touch device', () => {
  it('no key sends, the Send button does', () => {
    stubPointer(true)
    const p = props()
    render(<ChatInput {...p} />)
    expect(press({ shiftKey: true })).toBe(false)
    expect(press({ ctrlKey: true })).toBe(false)
    expect(press({})).toBe(false)
    expect(p.onSend).not.toHaveBeenCalled()
    const send = screen.getByRole('button', { name: 'Send' })
    expect(send).not.toHaveAttribute('title')
    fireEvent.click(send)
    expect(p.onSend).toHaveBeenCalledWith('hello')
  })

  it('the pointer is right on the first render (no desktop paint first)', () => {
    stubPointer(true)
    const { result } = renderHook(() => usePrimaryPointerCoarse())
    expect(result.current).toBe(true)
  })
})

describe('ChatInput: a paste never sends', () => {
  it('a large text paste with line breaks sends nothing, nor does a plain Enter after it', () => {
    const p = props()
    render(<ChatInput {...p} />)
    const big = Array.from({ length: 400 }, (_, i) => `line ${i}`).join('\n') + '\n'
    fireEvent.paste(box(), { clipboardData: { files: [], getData: () => big } })
    press({})
    expect(p.onSend).not.toHaveBeenCalled()
  })

  it('Ctrl still held from Ctrl+V: Ctrl+Enter adds a line, after the release it sends', () => {
    const p = props({ value: 'hello' })
    render(<ChatInput {...p} />)
    fireEvent.keyDown(box(), { key: 'Control', ctrlKey: true })
    fireEvent.keyDown(box(), { key: 'v', ctrlKey: true })
    textPaste()
    box().setSelectionRange(5, 5)
    expect(press({ ctrlKey: true })).toBe(true)
    expect(p.onSend).not.toHaveBeenCalled()
    expect(p.onChange).toHaveBeenLastCalledWith('hello\n')
    fireEvent.keyUp(box(), { key: 'Control', ctrlKey: false })
    press({ ctrlKey: true })
    expect(p.onSend).toHaveBeenCalledTimes(1)
  })

  it('Shift still held from Shift+Insert: Shift+Enter adds a line', () => {
    const p = props({ value: 'hello' })
    render(<ChatInput {...p} />)
    fireEvent.keyDown(box(), { key: 'Shift', shiftKey: true })
    fireEvent.keyDown(box(), { key: 'Insert', shiftKey: true })
    textPaste()
    box().setSelectionRange(5, 5)
    press({ shiftKey: true })
    expect(p.onSend).not.toHaveBeenCalled()
    expect(p.onChange).toHaveBeenLastCalledWith('hello\n')
  })

  it('a menu paste with nothing held leaves Shift+Enter a send', () => {
    const p = props()
    render(<ChatInput {...p} />)
    fireEvent.keyDown(box(), { key: 'a' })
    textPaste()
    press({ shiftKey: true })
    expect(p.onSend).toHaveBeenCalledTimes(1)
  })

  it('the guard expires on its own when the release is never seen', () => {
    vi.useFakeTimers()
    const p = props({ value: 'hello' })
    render(<ChatInput {...p} />)
    fireEvent.keyDown(box(), { key: 'Control', ctrlKey: true })
    textPaste()
    vi.advanceTimersByTime(PASTE_GUARD_MS - 100)
    press({ ctrlKey: true })
    expect(p.onSend).not.toHaveBeenCalled()
    vi.advanceTimersByTime(200)
    press({ ctrlKey: true })
    expect(p.onSend).toHaveBeenCalledTimes(1)
  })

  it('leaving the box disarms it', () => {
    const p = props()
    render(<ChatInput {...p} />)
    fireEvent.keyDown(box(), { key: 'Control', ctrlKey: true })
    textPaste()
    fireEvent.blur(box())
    press({ ctrlKey: true })
    expect(p.onSend).toHaveBeenCalledTimes(1)
  })
})

describe('ChatInput: Send beside Stop during a turn', () => {
  const turn = { streaming: true, queueable: true, onAbort: vi.fn() }

  it('shows with something to send, Stop square and still named Stop', () => {
    const p = props(turn)
    render(<ChatInput {...p} />)
    const stop = screen.getByRole('button', { name: 'Stop' })
    expect(stop).toHaveAttribute('title', 'Stop')
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(p.onSend).toHaveBeenCalledWith('hello')
    fireEvent.click(stop)
    expect(turn.onAbort).toHaveBeenCalled()
  })

  it.each([
    ['nothing to send', { value: '' }],
    ['the session still warming up', { queueable: false }],
    ['a stop under way', { aborting: true }],
    ['phone mode', { voice: { duplex: { active: true } } }],
  ])('hides with %s', (_name, over) => {
    render(<ChatInput {...props({ ...turn, ...over })} />)
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull()
    expect(screen.getByRole('button', { name: /Stop/ })).toBeTruthy()
  })
})

describe('composerKeys', () => {
  it('maps the keys', () => {
    const e = (over: Partial<KeyboardEvent>) => ({
      key: 'Enter', shiftKey: false, ctrlKey: false, metaKey: false, altKey: false,
      repeat: false, isComposing: false, keyCode: 13, ...over,
    })
    const fine = { coarse: false, pasteHeld: false }
    expect(enterAction(e({}), fine)).toBe('pass')
    expect(enterAction(e({ shiftKey: true }), fine)).toBe('send')
    expect(enterAction(e({ shiftKey: true }), { coarse: true, pasteHeld: false })).toBe('pass')
    expect(enterAction(e({ ctrlKey: true }), { coarse: false, pasteHeld: true })).toBe('newline')
    expect(enterAction(e({ key: 'a', shiftKey: true }), fine)).toBe('pass')
  })

  it('says how to send in the placeholder on a desktop only', () => {
    expect(withSendHint('Type a message', false)).toBe('Type a message, Shift+Enter to send')
    expect(withSendHint('Type to queue a message', true)).toBe('Type to queue a message...')
  })
})
