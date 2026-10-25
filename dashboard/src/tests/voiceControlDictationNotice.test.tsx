import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { VoiceControl } from '@/components/chat/VoiceControl'
import { MicIcon } from '@/components/chat/MicIcon'
import type { SpeechHandlers } from '@/hooks/useSpeechSession'

// ─── The composer says when dictation waits or stops ─────────────────────────
// A slow link's notice shows above the mic while recording; an error stays
// until the person taps it (a dictation that stopped on its own must not pass
// unseen while they talk).

const state = vi.hoisted(() => ({
  status: 'recording' as 'idle' | 'connecting' | 'recording' | 'finishing',
  handlers: [] as SpeechHandlers[],
}))
vi.mock('@/hooks/useSpeechSession', () => ({
  useSpeechSession: (h: SpeechHandlers) => {
    state.handlers.push(h)
    return { available: true, status: state.status, toggle: vi.fn(), stop: vi.fn(), start: vi.fn() }
  },
}))

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const latest = () => state.handlers[state.handlers.length - 1]

const SLOW = 'The connection is slow. Your words will appear when it catches up.'
const LOST = 'The connection was lost, so dictation stopped. What was heard is in the box. Tap the mic to continue.'
const FINISHING = 'Finishing the last words.'

beforeEach(() => {
  state.status = 'recording'
  state.handlers = []
  vi.useFakeTimers({ shouldAdvanceTime: true })
})
afterEach(() => {
  vi.useRealTimers()
})

describe('VoiceControl', () => {
  const props = {
    onDictateInterim: vi.fn(), onDictateFinal: vi.fn(), onDictateActive: vi.fn(),
    interruptSignal: 0, discardSignal: 0,
  }

  it('shows the slow notice while recording and hides it when cleared', () => {
    render(<VoiceControl {...props} />, { wrapper })
    act(() => { latest().onNotice?.(SLOW) })
    expect(screen.getByRole('status').textContent).toBe(SLOW)
    act(() => { latest().onNotice?.(null) })
    expect(screen.getByRole('status').textContent).toBe('')   // the live region stays, empty
  })

  it('keeps a dictation error until it is tapped or Escape dismisses it', () => {
    render(<VoiceControl {...props} />, { wrapper })
    act(() => { latest().onError?.(LOST) })
    act(() => { vi.advanceTimersByTime(30_000) })
    const error = screen.getByText(LOST, { selector: 'button' })
    fireEvent.click(error)
    expect(screen.queryByText(LOST, { selector: 'button' })).toBeNull()
    act(() => { latest().onError?.(LOST) })
    fireEvent.keyDown(screen.getByText(LOST, { selector: 'button' }), { key: 'Escape' })
    expect(screen.queryByText(LOST, { selector: 'button' })).toBeNull()
  })

  it('a send or a chat switch clears the sentence with the mic', () => {
    const { rerender } = render(<VoiceControl {...props} />, { wrapper })
    act(() => { latest().onError?.(LOST) })
    expect(screen.getByText(LOST, { selector: 'button' })).toBeTruthy()
    rerender(<VoiceControl {...props} discardSignal={1} />)
    expect(screen.queryByText(LOST, { selector: 'button' })).toBeNull()
  })

  it('shows the sentence in the composer\'s row when it is given one, the live region always there', () => {
    const slot = document.createElement('div')
    document.body.appendChild(slot)
    render(<VoiceControl {...props} sentenceSlot={slot} />, { wrapper })
    expect(slot.querySelector('[role="status"]')?.textContent).toBe('')   // mounted before it speaks
    act(() => { latest().onNotice?.(SLOW) })
    expect(slot.querySelector('[role="status"]')?.textContent).toBe(SLOW)
    act(() => { latest().onError?.(LOST) })
    expect(slot.querySelector('button')?.textContent).toBe(LOST)
    slot.remove()
  })
})

describe('MicIcon', () => {
  it('shows the notice while recording and keeps the error', () => {
    render(<MicIcon onTranscript={vi.fn()} />, { wrapper })
    act(() => { latest().onNotice?.(SLOW) })
    expect(screen.getByRole('status').textContent).toBe(SLOW)
    act(() => { latest().onError?.(LOST) })
    act(() => { vi.advanceTimersByTime(30_000) })
    expect(screen.getByText(LOST, { selector: 'button' })).toBeTruthy()
    expect(screen.getByRole('status').textContent).toBe('')   // the error speaks, the notice steps aside
  })

  it('shows the finishing notice while the server ends the dictation, none once idle', () => {
    state.status = 'finishing'
    const { rerender } = render(<MicIcon onTranscript={vi.fn()} />, { wrapper })
    act(() => { latest().onNotice?.(FINISHING) })
    expect(screen.getByRole('status').textContent).toBe(FINISHING)
    state.status = 'idle'
    rerender(<MicIcon onTranscript={vi.fn()} />)
    expect(screen.getByRole('status').textContent).toBe('')
  })
})
