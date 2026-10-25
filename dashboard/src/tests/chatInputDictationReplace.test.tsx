// The platform engine can move a dictation to another language while the
// person speaks: a replace carries the new language's reading of the words
// the session placed. The composer rewrites exactly those words, keeps what
// was typed before the session, and ignores a replace once the person edited
// the dictated text.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act, fireEvent } from '@testing-library/react'

type Handlers = {
  onInterim?: (t: string) => void
  onFinal?: (t: string) => void
  onReplace?: (t: string) => void
  onActive?: (a: boolean) => void
}

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

function baseProps(onChange: (t: string) => void, value = '') {
  return {
    value,
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

describe('ChatInput dictation replace', () => {
  beforeEach(() => {
    speech.handlers = null
    speech.stop.mockClear()
  })

  for (const voice of [true, false]) {
    const host = voice ? 'VoiceControl' : 'MicIcon'

    it(`rewrites the words the session placed and dictation continues after them (${host})`, () => {
      const onChange = vi.fn()
      const props = { ...baseProps(onChange), ...(voice ? { voice: {} } : {}) }
      const view = render(<ChatInput {...props} />)
      const h = speech.handlers!
      act(() => { h.onActive?.(true); h.onFinal?.('yes sir') })
      expect(onChange).toHaveBeenLastCalledWith('yes sir')
      view.rerender(<ChatInput {...props} value="yes sir" />)
      act(() => { h.onReplace?.('Ναι κύριε') })
      expect(onChange).toHaveBeenLastCalledWith('Ναι κύριε')
      view.rerender(<ChatInput {...props} value="Ναι κύριε" />)
      act(() => { h.onInterim?.('τι') })
      expect(onChange).toHaveBeenLastCalledWith('Ναι κύριε τι')
      act(() => { h.onFinal?.('τι κάνετε') })
      expect(onChange).toHaveBeenLastCalledWith('Ναι κύριε τι κάνετε')
    })

    it(`keeps the text typed before the session and an empty replace clears the placed words (${host})`, () => {
      const onChange = vi.fn()
      const props = { ...baseProps(onChange, 'Note:'), ...(voice ? { voice: {} } : {}) }
      const view = render(<ChatInput {...props} />)
      const h = speech.handlers!
      act(() => { h.onActive?.(true); h.onFinal?.('hello there') })
      expect(onChange).toHaveBeenLastCalledWith('Note: hello there')
      view.rerender(<ChatInput {...props} value="Note: hello there" />)
      act(() => { h.onReplace?.('') })
      expect(onChange).toHaveBeenLastCalledWith('Note:')
      view.rerender(<ChatInput {...props} value="Note:" />)
      act(() => { h.onReplace?.('γεια σας') })
      expect(onChange).toHaveBeenLastCalledWith('Note: γεια σας')
    })

    it(`splices the new words in and keeps what was typed after them (${host})`, () => {
      const onChange = vi.fn()
      const props = { ...baseProps(onChange), ...(voice ? { voice: {} } : {}) }
      const view = render(<ChatInput {...props} />)
      const h = speech.handlers!
      act(() => { h.onActive?.(true); h.onFinal?.('yes sir') })
      view.rerender(<ChatInput {...props} value="yes sir" />)
      const textarea = view.getByRole('textbox') as HTMLTextAreaElement
      fireEvent.change(textarea, { target: { value: 'yes sir, and more' } })
      view.rerender(<ChatInput {...props} value="yes sir, and more" />)
      act(() => { h.onReplace?.('Ναι κύριε') })
      expect(onChange).toHaveBeenLastCalledWith('Ναι κύριε, and more')
      view.rerender(<ChatInput {...props} value="Ναι κύριε, and more" />)
      act(() => { h.onReplace?.('') })
      expect(onChange).toHaveBeenLastCalledWith(', and more')
    })

    it(`appends a replace once the placed words themselves were edited (${host})`, () => {
      const onChange = vi.fn()
      const props = { ...baseProps(onChange), ...(voice ? { voice: {} } : {}) }
      const view = render(<ChatInput {...props} />)
      const h = speech.handlers!
      act(() => { h.onActive?.(true); h.onFinal?.('yes sir') })
      view.rerender(<ChatInput {...props} value="yes sir" />)
      const textarea = view.getByRole('textbox') as HTMLTextAreaElement
      fireEvent.change(textarea, { target: { value: 'yes, sir and more' } })
      view.rerender(<ChatInput {...props} value="yes, sir and more" />)
      act(() => { h.onReplace?.('Ναι κύριε') })
      expect(onChange).toHaveBeenLastCalledWith('yes, sir and more Ναι κύριε')
    })

    it(`keeps a line break before the dictation through a replace (${host})`, () => {
      const onChange = vi.fn()
      const props = { ...baseProps(onChange, 'Line1\n'), ...(voice ? { voice: {} } : {}) }
      const view = render(<ChatInput {...props} />)
      const h = speech.handlers!
      act(() => { h.onActive?.(true); h.onFinal?.('γεια') })
      expect(onChange).toHaveBeenLastCalledWith('Line1\nγεια')
      view.rerender(<ChatInput {...props} value={'Line1\nγεια'} />)
      act(() => { h.onReplace?.('γεια σας') })
      expect(onChange).toHaveBeenLastCalledWith('Line1\nγεια σας')
    })
  }
})
