import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { VoiceControl, type DuplexControlProps } from '@/components/chat/VoiceControl'

// ─── VoiceControl duplex rendering (speech session mocked) ───────────────────
//
// The phone button renders ONLY on a positive server capability (fail-closed);
// while a duplex session is active the mic button is the MUTE TOGGLE
// (operator UX decision 2026-08-12 — barge-in is voice-first, the old
// tap-to-interrupt stop square is gone), and captions render in the composer.

vi.mock('@/hooks/useSpeechSession', () => ({
  useSpeechSession: () => ({
    available: true, status: 'idle',
    start: vi.fn(), stop: vi.fn(), toggle: vi.fn(),
  }),
}))

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

describe('VoiceControl duplex', () => {
  it('renders no phone button without the capability', () => {
    render(<VoiceControl {...baseProps} />)
    expect(screen.queryByTitle(/full duplex/i)).toBeNull()
    render(<VoiceControl {...baseProps} duplex={dup({ available: false })} />)
    expect(screen.queryByTitle(/full duplex/i)).toBeNull()
  })

  it('phone button starts and ends the session', () => {
    const d = dup()
    render(<VoiceControl {...baseProps} duplex={d} />)
    fireEvent.click(screen.getByTitle(/full duplex/i))
    expect(d.onToggle).toHaveBeenCalledTimes(1)

    const active = dup({ active: true, phase: 'listening' })
    render(<VoiceControl {...baseProps} duplex={active} />)
    fireEvent.click(screen.getByTitle(/end the live conversation/i))
    expect(active.onToggle).toHaveBeenCalledTimes(1)
  })

  it('mic tap toggles mute in every live phase — never a barge-in', () => {
    // Listening: tap mutes.
    const listeningDup = dup({ active: true, phase: 'listening' })
    render(<VoiceControl {...baseProps} duplex={listeningDup} />)
    fireEvent.click(screen.getByTitle(/tap to mute/i))
    expect(listeningDup.onToggleMute).toHaveBeenCalledTimes(1)

    // Speaking: same control — no stop square, no interrupt.
    const speakingDup = dup({ active: true, phase: 'speaking' })
    render(<VoiceControl {...baseProps} duplex={speakingDup} />)
    const muteBtns = screen.getAllByTitle(/tap to mute/i)
    fireEvent.click(muteBtns[muteBtns.length - 1])
    expect(speakingDup.onToggleMute).toHaveBeenCalledTimes(1)

    // Muted: distinct affordance, tap unmutes.
    const mutedDup = dup({ active: true, phase: 'listening', muted: true })
    render(<VoiceControl {...baseProps} duplex={mutedDup} />)
    const unmuteBtn = screen.getByTitle(/tap to unmute/i)
    expect(unmuteBtn.getAttribute('aria-pressed')).toBe('true')
    // Muted = slashed glyph alone, no colored chip (operator decision
    // 2026-08-14): red fill means "live and hearing you".
    expect(unmuteBtn.className).not.toMatch(/bg-red|bg-p-surface-hover|animate-pulse/)
    fireEvent.click(unmuteBtn)
    expect(mutedDup.onToggleMute).toHaveBeenCalledTimes(1)
  })

  it('feeds the live caption into the composer display path', () => {
    const onDictateInterim = vi.fn()
    const onDictateActive = vi.fn()
    render(<VoiceControl {...baseProps}
      onDictateInterim={onDictateInterim} onDictateActive={onDictateActive}
      duplex={dup({ active: true, phase: 'listening', caption: 'turn on the lights' })} />)
    expect(onDictateActive).toHaveBeenCalledWith(true)
    expect(onDictateInterim).toHaveBeenCalledWith('turn on the lights')
  })

  it('surfaces the end reason on error', () => {
    render(<VoiceControl {...baseProps}
      duplex={dup({ phase: 'error', endReason: 'duplex engine not connected' })} />)
    expect(screen.getByText('duplex engine not connected')).toBeTruthy()
  })
})
