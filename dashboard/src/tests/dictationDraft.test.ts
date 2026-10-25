import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useDictationDraft, findWords } from '@/hooks/useDictationDraft'
import { waitForStopFinal, STOP_FINAL_WAIT_MS } from '@/audio/backends/platformStt'

// ─── The composer's dictation draft: a language switch's splice ─────────────

function draft(initial = '') {
  const state = { text: initial }
  const valueRef = { current: initial as string | undefined }
  const draftKeyRef = { current: 'chat-1' as string | undefined }
  const { result } = renderHook(() => useDictationDraft({
    valueRef, draftKeyRef,
    setText: (t: string) => { state.text = t; valueRef.current = t },
    onStart: () => {}, afterChange: () => {},
  }))
  // The person typing: the composer's text changes and the hook re-baselines.
  const type = (value: string) => { state.text = value; valueRef.current = value; result.current.rebaseline(value) }
  return { state, result, type }
}

describe('the dictation draft splice', () => {
  it('replaces the placed words where the session put them, not where they last appear', () => {
    const { state, result, type } = draft()
    act(() => { result.current.onMicActive(true) })
    act(() => { result.current.appendTranscript('no') })
    type('no not now')                                   // typed text that contains the placed word
    act(() => { result.current.replaceTranscript('Όχι') })
    expect(state.text).toBe('Όχι not now')
  })

  it('follows the placed words when text is typed before them', () => {
    const { state, result, type } = draft('Hi')
    act(() => { result.current.onMicActive(true) })
    act(() => { result.current.appendTranscript('yes sir') })
    expect(state.text).toBe('Hi yes sir')
    type('Hello there, yes sir')
    act(() => { result.current.replaceTranscript('ναι κύριε') })
    expect(state.text).toBe('Hello there, ναι κύριε')
  })

  it('finds the placed words on word boundaries when the offset no longer holds', () => {
    expect(findWords('know no now', 'no')).toBe(5)
    expect(findWords('notes', 'no')).toBe(-1)
    expect(findWords('a, no.', 'no')).toBe(3)
  })

  it('appends the new reading when the placed words were edited away', () => {
    const { state, result, type } = draft()
    act(() => { result.current.onMicActive(true) })
    act(() => { result.current.appendTranscript('call him') })
    type('call them')
    act(() => { result.current.replaceTranscript('κάλεσέ τον') })
    expect(state.text).toBe('call them κάλεσέ τον')
    act(() => { result.current.appendTranscript('τώρα') })
    act(() => { result.current.replaceTranscript('κάλεσέ τον τώρα') })
    expect(state.text).toBe('call them κάλεσέ τον τώρα')
  })

  it('keeps splicing after a replace and the finals that follow it', () => {
    const { state, result } = draft()
    act(() => { result.current.onMicActive(true) })
    act(() => { result.current.appendTranscript('kali') })
    act(() => { result.current.replaceTranscript('Καλη') })
    act(() => { result.current.appendTranscript('μέρα') })
    act(() => { result.current.replaceTranscript('Καλημέρα σας') })
    expect(state.text).toBe('Καλημέρα σας')
  })
})

// ─── The stop waits for the server's last final ─────────────────────────────

describe('the platform engine stop', () => {
  it('waits up to the bound for the final, which a switch still deciding can delay', async () => {
    vi.useFakeTimers()
    try {
      const sock = new EventTarget() as unknown as WebSocket
      let done = false
      void waitForStopFinal(sock).then(() => { done = true })
      await vi.advanceTimersByTimeAsync(3000)           // longer than the old 1.8 s bound
      expect(done).toBe(false)
      sock.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ type: 'final', text: 'x' }) }))
      await vi.advanceTimersByTimeAsync(0)
      expect(done).toBe(true)
      let bounded = false
      void waitForStopFinal(new EventTarget() as unknown as WebSocket).then(() => { bounded = true })
      await vi.advanceTimersByTimeAsync(STOP_FINAL_WAIT_MS)
      expect(bounded).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })
})
