// Dictation is bound to the chat it started in. A chat switch (a new draftKey)
// stops the mic and drops its tail, and a phrase still in flight never lands
// under the new key — dictation used to keep listening across a switch and
// write the whole accumulated transcript into the chat switched to.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act } from '@testing-library/react'

type Handlers = {
  onInterim?: (t: string) => void
  onFinal?: (t: string) => void
  onActive?: (a: boolean) => void
}

// The speech session is mocked at the hook: the test drives its handlers as
// the recognizer would, and `stop` records how the mic was closed.
const speech = vi.hoisted(() => ({
  handlers: null as Handlers | null,
  stop: vi.fn(),
}))

vi.mock('@/hooks/useSpeechSession', () => ({
  useSpeechSession: (h: Handlers) => {
    speech.handlers = h
    return { available: true, status: 'recording', start: vi.fn(), stop: speech.stop, toggle: vi.fn() }
  },
}))

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { feature_flags: { upload_max_bytes: 1000 } } }),
}))

vi.mock('@/components/chat/media/ImageLightbox', () => ({ default: () => null }))

import ChatInput from '@/components/chat/ChatInput'
import type { PendingFile } from '@/store/types'

function baseProps(onChange: (t: string) => void) {
  return {
    value: '',
    onChange,
    onSend: () => {},
    pendingImages: [],
    onAddImages: vi.fn(),
    onRemoveImage: () => {},
    pendingFiles: [] as PendingFile[],
    onAddFiles: vi.fn(),
    onRemoveFile: vi.fn(),
    onRetryFile: vi.fn(),
    draftKey: 'chat-1',
  }
}

// Start dictating in chat-1, commit one phrase, then switch to chat-2 whose
// restored draft arrives in the same render. Both mic hosts (VoiceControl
// with the voice prop, MicIcon without) must behave the same.
function dictateThenSwitch(voice: boolean) {
  const onChange = vi.fn()
  const props = { ...baseProps(onChange), ...(voice ? { voice: {} } : {}) }
  const view = render(<ChatInput {...props} />)
  const h = speech.handlers!
  act(() => { h.onActive?.(true); h.onFinal?.('hello') })
  expect(onChange).toHaveBeenLastCalledWith('hello')
  expect(speech.stop).not.toHaveBeenCalled()
  const calls = onChange.mock.calls.length

  view.rerender(<ChatInput {...props} value="restored" draftKey="chat-2" />)
  expect(speech.stop).toHaveBeenCalledWith(true)

  // A late final and interim from the old attempt, before the stop lands.
  const late = speech.handlers!
  act(() => { late.onFinal?.('late'); late.onInterim?.('partial') })
  expect(onChange.mock.calls.length).toBe(calls)
}

describe('ChatInput dictation across a chat switch', () => {
  beforeEach(() => {
    speech.handlers = null
    speech.stop.mockClear()
  })

  it('stops the mic, drops the tail and never writes under the new key (VoiceControl)', () => {
    dictateThenSwitch(true)
  })

  it('stops the mic, drops the tail and never writes under the new key (MicIcon)', () => {
    dictateThenSwitch(false)
  })

  it('leaves a running dictation alone when the key does not change', () => {
    const onChange = vi.fn()
    const props = { ...baseProps(onChange), voice: {} }
    const view = render(<ChatInput {...props} />)
    act(() => { speech.handlers!.onActive?.(true); speech.handlers!.onFinal?.('hello') })
    view.rerender(<ChatInput {...props} value="hello" />)
    expect(speech.stop).not.toHaveBeenCalled()
    act(() => { speech.handlers!.onFinal?.('again') })
    expect(onChange).toHaveBeenLastCalledWith('hello again')
  })
})
