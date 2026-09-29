import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { VoiceControl, type DuplexControlProps } from '@/components/chat/VoiceControl'
import { LANGUAGES } from '@/audio/lang'
import * as authApi from '@/api/auth'

// ─── VoiceControl — hold the mic for the dictation language ─────────────────
//
// A 500 ms hold (or a right-click) on the mic opens the language menu with
// the stored preference ticked; a pick PUTs the audio prefs and closes it.
// The hold never toggles dictation, a slide never opens the menu, and a tap
// stays a tap.

const speech = vi.hoisted(() => ({ toggle: vi.fn(), stop: vi.fn(), start: vi.fn() }))
vi.mock('@/hooks/useSpeechSession', () => ({
  useSpeechSession: () => ({ available: true, status: 'idle', ...speech }),
}))

const fetchSpy = vi.spyOn(authApi, 'apiFetch')
const PREFS = { stt_mode: 'auto', tts_mode: 'auto', tts_voice_map: {}, stt_language: 'el-GR' }

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const baseProps = {
  onDictateInterim: vi.fn(), onDictateFinal: vi.fn(), onDictateActive: vi.fn(),
  interruptSignal: 0, discardSignal: 0,
}

function dup(over: Partial<DuplexControlProps> = {}): DuplexControlProps {
  return {
    available: true, active: false, phase: 'off', caption: '', endReason: '',
    onToggle: vi.fn(), onToggleMute: vi.fn(), ...over,
  }
}

const mic = () => screen.getByTitle(/^Dictate/)
const menu = () => screen.queryByText('Dictation language')
const down = (el: HTMLElement, x = 100, y = 100) =>
  fireEvent.pointerDown(el, { pointerId: 1, clientX: x, clientY: y, button: 0 })
const hold = () => act(() => { vi.advanceTimersByTime(600) })

beforeEach(() => {
  // The fake clock keeps moving with real time so react-query's scheduling
  // and waitFor still run; the hold itself is jumped explicitly.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      return { ok: true, json: async () => ({ ...PREFS, ...JSON.parse(String(init.body)) }) } as Response
    }
    if (path === '/v1/users/me/audio-prefs') return { ok: true, json: async () => PREFS } as Response
    return { ok: true, json: async () => ({}) } as Response
  })
})
afterEach(() => {
  vi.useRealTimers()
  fetchSpy.mockReset()
  speech.toggle.mockReset()
  speech.stop.mockReset()
})

describe('VoiceControl — dictation language menu', () => {
  it('a hold opens the menu with every language and the stored one ticked', async () => {
    render(<VoiceControl {...baseProps} />, { wrapper })
    expect(mic().getAttribute('title')).toBe('Dictate (hold for the language)')
    down(mic())
    expect(menu()).toBeNull()
    hold()
    expect(menu()).toBeTruthy()
    for (const l of LANGUAGES) expect(screen.getByText(l.label)).toBeTruthy()

    const greek = await waitFor(() => {
      const row = screen.getByText('Greek').closest('button')!
      expect(row.className).toContain('text-brand')
      return row
    })
    expect(greek.querySelector('svg')).toBeTruthy()
    expect(screen.getByText('English (US)').closest('button')!.className).not.toContain('text-brand')
    // The trailing click of the hold is not a tap: dictation stays off.
    fireEvent.pointerUp(mic(), { pointerId: 1 })
    fireEvent.click(mic())
    expect(speech.toggle).not.toHaveBeenCalled()
    expect(menu()).toBeTruthy()
    // No live conversation: no footer.
    expect(screen.queryByText(/next conversation/)).toBeNull()
  })

  it('picking a row saves the language and closes the menu', async () => {
    render(<VoiceControl {...baseProps} />, { wrapper })
    down(mic())
    hold()
    fireEvent.click(screen.getByText('German'))
    expect(menu()).toBeNull()
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledWith('/v1/users/me/audio-prefs', {
      method: 'PUT', body: JSON.stringify({ stt_language: 'de-DE' }),
    }))
    expect(speech.toggle).not.toHaveBeenCalled()
  })

  it('a slide never opens the menu', () => {
    const d = dup()
    render(<VoiceControl {...baseProps} duplex={d} />, { wrapper })
    down(mic(), 100, 100)
    fireEvent.pointerMove(mic(), { pointerId: 1, clientX: 60, clientY: 100 })
    expect(d.onToggle).toHaveBeenCalledTimes(1)  // the slide still enables duplex
    hold()
    expect(menu()).toBeNull()
    fireEvent.pointerUp(mic(), { pointerId: 1 })
    fireEvent.click(mic())
    expect(speech.toggle).not.toHaveBeenCalled()
  })

  it('a small drift disarms the hold without sliding', () => {
    const d = dup()
    render(<VoiceControl {...baseProps} duplex={d} />, { wrapper })
    down(mic(), 100, 100)
    fireEvent.pointerMove(mic(), { pointerId: 1, clientX: 100, clientY: 112 })
    hold()
    expect(menu()).toBeNull()
    expect(d.onToggle).not.toHaveBeenCalled()
  })

  it('a tap toggles dictation and never opens the menu', () => {
    render(<VoiceControl {...baseProps} />, { wrapper })
    down(mic())
    fireEvent.pointerUp(mic(), { pointerId: 1 })
    fireEvent.click(mic())
    expect(speech.toggle).toHaveBeenCalledTimes(1)
    hold()
    expect(menu()).toBeNull()
  })

  it('a right-click opens the menu and suppresses the browser menu', () => {
    render(<VoiceControl {...baseProps} />, { wrapper })
    const notCancelled = fireEvent.contextMenu(mic())
    expect(notCancelled).toBe(false)
    expect(menu()).toBeTruthy()
    // A mousedown outside the control closes it; so does Escape.
    fireEvent.mouseDown(document.body)
    expect(menu()).toBeNull()
    fireEvent.contextMenu(mic())
    expect(menu()).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(menu()).toBeNull()
  })

  it('a disabled mic opens nothing', () => {
    render(<VoiceControl {...baseProps} disabled />, { wrapper })
    down(mic())
    hold()
    expect(menu()).toBeNull()
    fireEvent.contextMenu(mic())
    expect(menu()).toBeNull()
  })

  it('says the pick applies to the next conversation while duplex is live', () => {
    render(<VoiceControl {...baseProps} duplex={dup({ active: true, phase: 'listening' })} />, { wrapper })
    fireEvent.contextMenu(screen.getByTitle(/tap to mute/i))
    expect(menu()).toBeTruthy()
    expect(screen.getByText('Applies to your next conversation')).toBeTruthy()
  })
})
