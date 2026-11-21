import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render } from '@testing-library/react'

// The composer grows with its text to 10 rows while focused and shrinks to 4
// when the focus leaves it. A press elsewhere shrinks it on the task after the
// release (the click lands first), a press on the composer's own bar keeps it
// open, focus moving into a frame shrinks it once the press inside has had
// time to land (on a touch screen at once), a scroll (pointercancel) or a
// window blur changes nothing, and every height write other than a focus
// change is instant.

vi.mock('@/hooks/useSpeechSession', () => ({
  useSpeechSession: () => ({ available: false, status: 'idle', start: vi.fn(), stop: vi.fn(), toggle: vi.fn() }),
}))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: null }) }))
vi.mock('@/components/chat/media/ImageLightbox', () => ({ default: () => null }))
vi.mock('@/components/chat/PresenceHalo', () => ({ PresenceHalo: () => null }))
const mic: { onActive?: (a: boolean) => void } = {}
vi.mock('@/components/chat/MicIcon', () => ({
  MicIcon: (p: { onActive?: (a: boolean) => void }) => { mic.onActive = p.onActive; return null },
}))
const voiceCtl: { onDictateActive?: (a: boolean) => void } = {}
vi.mock('@/components/chat/VoiceControl', () => ({
  VoiceControl: (p: { onDictateActive?: (a: boolean) => void }) => { voiceCtl.onDictateActive = p.onDictateActive; return null },
}))

import ChatInput from '@/components/chat/ChatInput'
import { HEIGHT_TRANSITION, rowsPx } from '@/hooks/useComposerHeight'

let content = 300
beforeEach(() => {
  content = 300
  Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', {
    configurable: true,
    get() { return (this as HTMLTextAreaElement).style.height === 'auto' || content > parseFloat((this as HTMLTextAreaElement).style.height || '0') ? content : parseFloat((this as HTMLTextAreaElement).style.height) },
  })
  vi.useFakeTimers()
})
afterEach(() => {
  delete (HTMLTextAreaElement.prototype as { scrollHeight?: number }).scrollHeight
  delete (window as { matchMedia?: unknown }).matchMedia
  delete (document as { activeElement?: unknown }).activeElement
  vi.useRealTimers()
})

function props(over: Record<string, unknown> = {}) {
  return {
    value: 'a long draft',
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

function page(over: Record<string, unknown> = {}) {
  const utils = render(
    <div>
      <button data-testid="outside">history</button>
      <iframe data-testid="frame" title="artifact" />
      <div data-composer-bar>
        <button data-testid="picker">Model</button>
        <ChatInput {...props(over)} />
      </div>
    </div>,
  )
  const box = utils.container.querySelector('textarea') as HTMLTextAreaElement
  const get = (id: string) => utils.getByTestId(id)
  return { ...utils, box, get }
}

const px = (box: HTMLTextAreaElement, rows: number) => `${Math.min(content, rowsPx(box, rows))}px`
const flush = () => act(() => { vi.runOnlyPendingTimers() })

/** A mouse click elsewhere: the focus moves at pointerdown, the click lands
 * at pointerup. */
function clickOrder(target: HTMLElement) {
  fireEvent.pointerDown(target)
  act(() => target.focus())
}

function focusBox(box: HTMLTextAreaElement) {
  act(() => box.focus())
}

/** A press or a tap inside a frame, as Chrome shows it to the page: the
 * focused element blurs with no related target, the window blurs with the
 * frame already active, and nothing else (no press, no focus event on the
 * frame element). */
function focusIntoFrame(from: HTMLElement, frame: HTMLElement) {
  Object.defineProperty(document, 'activeElement', { configurable: true, get: () => frame })
  act(() => {
    fireEvent.blur(from)
    fireEvent.focusOut(from)
    fireEvent.blur(window)
  })
}

function backFromFrame(box: HTMLTextAreaElement) {
  delete (document as { activeElement?: unknown }).activeElement
  focusBox(box)
}

const wait = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })

function touchScreen() {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: q === '(pointer: coarse)', media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia
}

describe('ChatInput height', () => {
  it('a chat opened with the composer empty paints one row, with no transition', () => {
    content = 36
    const { box } = page({ value: '' })
    expect(box.style.height).toBe('36px')
    expect(box.style.transition).toBe('')
  })

  it('a long draft restored into an unfocused composer shows four rows, instantly', () => {
    const { box } = page()
    expect(box.style.height).toBe(px(box, 4))
    expect(box.style.transition).toBe('')
  })

  it('grows to ten rows on focus, animated', () => {
    const { box } = page()
    focusBox(box)
    expect(box.style.height).toBe(px(box, 10))
    expect(box.style.transition).toBe(HEIGHT_TRANSITION)
  })

  it('a click elsewhere shrinks it on the task after the release, not before', () => {
    const { box, get } = page()
    focusBox(box)
    clickOrder(get('outside'))
    expect(box.style.height).toBe(px(box, 10))
    fireEvent.pointerUp(get('outside'))
    expect(box.style.height).toBe(px(box, 10))
    flush()
    expect(box.style.height).toBe(px(box, 4))
    expect(box.style.transition).toBe(HEIGHT_TRANSITION)
  })

  it('a tap elsewhere (release before the focus moves) shrinks it', () => {
    const { box, get } = page()
    focusBox(box)
    fireEvent.pointerDown(get('outside'))
    fireEvent.pointerUp(get('outside'))
    act(() => get('outside').focus())
    flush()
    expect(box.style.height).toBe(px(box, 4))
  })

  it('a click or a tap on the composer bar keeps it open, a later click elsewhere shrinks it', () => {
    const { box, get } = page()
    focusBox(box)
    clickOrder(get('picker'))
    fireEvent.pointerUp(get('picker'))
    flush()
    expect(box.style.height).toBe(px(box, 10))
    // the touch order on the bar: Safari never focuses the button
    focusBox(box)
    fireEvent.pointerDown(get('picker'))
    fireEvent.pointerUp(get('picker'))
    act(() => box.blur())
    flush()
    expect(box.style.height).toBe(px(box, 10))
    fireEvent.pointerDown(get('outside'))
    fireEvent.pointerUp(get('outside'))
    flush()
    expect(box.style.height).toBe(px(box, 4))
  })

  it('the keyboard closing right after a tap on the box shrinks it (iOS Done)', () => {
    const { box } = page()
    fireEvent.pointerDown(box)
    fireEvent.pointerUp(box)
    focusBox(box)
    flush()
    expect(box.style.height).toBe(px(box, 10))
    act(() => box.blur())
    flush()
    expect(box.style.height).toBe(px(box, 4))
  })

  it('a scroll gesture (pointercancel) shrinks nothing', () => {
    const { box, get } = page()
    focusBox(box)
    clickOrder(get('outside'))
    fireEvent.pointerCancel(get('outside'))
    flush()
    expect(box.style.height).toBe(px(box, 10))
  })

  it('a click into a frame (an app, an artifact, a document) shrinks it once the click has landed', () => {
    const { box, get } = page()
    focusBox(box)
    focusIntoFrame(box, get('frame'))
    wait(0)
    expect(box.style.height).toBe(px(box, 10))
    wait(498)
    expect(box.style.height).toBe(px(box, 10))
    wait(2)
    expect(box.style.height).toBe(px(box, 4))
    expect(box.value).toBe('a long draft')
  })

  it('on a touch screen a tap into a frame shrinks it at once (the focus moves after the release)', () => {
    touchScreen()
    const { box, get } = page()
    focusBox(box)
    focusIntoFrame(box, get('frame'))
    flush()
    expect(box.style.height).toBe(px(box, 4))
  })

  it('back in the box before the wait ends, it stays open', () => {
    const { box, get } = page()
    focusBox(box)
    focusIntoFrame(box, get('frame'))
    wait(200)
    backFromFrame(box)
    wait(1000)
    expect(box.style.height).toBe(px(box, 10))
  })

  it('a control of the bar focused, then a click into a frame, shrinks it', () => {
    const { box, get } = page()
    focusBox(box)
    act(() => get('picker').focus())
    flush()
    expect(box.style.height).toBe(px(box, 10))
    focusIntoFrame(get('picker'), get('frame'))
    wait(500)
    expect(box.style.height).toBe(px(box, 4))
  })

  it('the window losing focus changes nothing', () => {
    const { box } = page()
    focusBox(box)
    act(() => {
      fireEvent.blur(box)
      fireEvent.blur(window)
    })
    flush()
    wait(1000)
    expect(box.style.height).toBe(px(box, 10))
  })

  it('Tab away shrinks it at once, Tab onto the bar keeps it', () => {
    const { box, get } = page()
    focusBox(box)
    act(() => get('picker').focus())
    flush()
    expect(box.style.height).toBe(px(box, 10))
    act(() => get('outside').focus())
    flush()
    expect(box.style.height).toBe(px(box, 4))
  })

  it('a press on the collapsed text grows it after the release, or at the first key', () => {
    const { box } = page()
    fireEvent.pointerDown(box)
    focusBox(box)
    expect(box.style.height).toBe(px(box, 4))
    fireEvent.pointerUp(box)
    flush()
    expect(box.style.height).toBe(px(box, 10))
    act(() => box.blur())
    flush()
    fireEvent.pointerDown(box)
    focusBox(box)
    fireEvent.keyDown(box, { key: 'a' })
    expect(box.style.height).toBe(px(box, 10))
  })

  it('a draft changed while collapsed refits at four rows, instantly', () => {
    const { box, rerender } = page()
    content = 500
    rerender(
      <div>
        <div data-composer-bar><ChatInput {...props({ value: 'another chat, longer draft', draftKey: 'c2' })} /></div>
      </div>,
    )
    expect(box.style.height).toBe(px(box, 4))
    expect(box.style.transition).toBe('')
  })

  it('no animation where the host turns it off, or under reduced motion', () => {
    const a = page({ animateHeight: false })
    focusBox(a.box)
    expect(a.box.style.height).toBe(px(a.box, 10))
    expect(a.box.style.transition).toBe('')
    a.unmount()
    window.matchMedia = vi.fn().mockImplementation((q: string) => ({
      matches: q === '(prefers-reduced-motion: reduce)', media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })) as unknown as typeof window.matchMedia
    const b = page()
    focusBox(b.box)
    expect(b.box.style.transition).toBe('')
  })

  it('dictation grows it, phone mode does not', () => {
    const a = page()
    act(() => mic.onActive?.(true))
    expect(a.box.style.height).toBe(px(a.box, 10))
    a.unmount()
    const b = page({ voice: { duplex: { active: true } } })
    act(() => voiceCtl.onDictateActive?.(true))
    expect(b.box.style.height).toBe(px(b.box, 4))
  })

  it('the photo lightbox open keeps it open', () => {
    const { box, get, getByLabelText } = page({ pendingImages: [{ id: 'i1', base64: 'data:image/png;base64,AA', name: 'p.png' }] })
    focusBox(box)
    fireEvent.click(getByLabelText('Preview p.png'))
    fireEvent.pointerDown(get('outside'))
    fireEvent.pointerUp(get('outside'))
    flush()
    expect(box.style.height).toBe(px(box, 10))
    focusIntoFrame(box, get('frame'))
    wait(1000)
    expect(box.style.height).toBe(px(box, 10))
  })

  it('unmounting removes its listeners and drops a pending change', () => {
    const removeDoc = vi.spyOn(document, 'removeEventListener')
    const removeWin = vi.spyOn(window, 'removeEventListener')
    const { box, get, unmount } = page()
    focusBox(box)
    focusIntoFrame(box, get('frame'))
    unmount()
    expect(removeDoc).toHaveBeenCalledWith('pointerdown', expect.any(Function), true)
    expect(removeDoc).toHaveBeenCalledWith('focusin', expect.any(Function), true)
    expect(removeWin).toHaveBeenCalledWith('pointerup', expect.any(Function), true)
    expect(removeWin).toHaveBeenCalledWith('blur', expect.any(Function))
    expect(() => vi.runOnlyPendingTimers()).not.toThrow()
  })
})

describe('the placeholder', () => {
  it('reads its hint by default, and nothing when the page says the box is locked above it', () => {
    const open = page({ placeholder: undefined })
    expect(open.box.placeholder).toBe('Type a message...')
    open.unmount()
    const locked = page({ placeholder: '', disabled: true })
    expect(locked.box.placeholder).toBe('')
    expect(locked.box.disabled).toBe(true)
  })
})
