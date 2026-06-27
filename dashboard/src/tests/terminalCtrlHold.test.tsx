import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import { applyCtrlHold, isCtrlHeld, setCtrlHold } from '@/lib/terminalCtrlHold'
import TerminalControlBar from '@/components/chat/terminal/TerminalControlBar'

afterEach(() => {
  setCtrlHold(false)
  cleanup()
})

describe('applyCtrlHold — one-shot sticky Ctrl', () => {
  it('passes input through untouched while disarmed', () => {
    expect(applyCtrlHold('c')).toBe('c')
  })

  it('maps the next letter to its control byte and disarms', () => {
    setCtrlHold(true)
    expect(applyCtrlHold('c')).toBe('\x03')
    expect(isCtrlHeld()).toBe(false)
    expect(applyCtrlHold('c')).toBe('c') // consumed — back to plain input
  })

  it('uppercase letters and the classic @…_ range map too', () => {
    setCtrlHold(true)
    expect(applyCtrlHold('D')).toBe('\x04')
    setCtrlHold(true)
    expect(applyCtrlHold('[')).toBe('\x1b')
  })

  it('Space → NUL and ? → DEL', () => {
    setCtrlHold(true)
    expect(applyCtrlHold(' ')).toBe('\x00')
    setCtrlHold(true)
    expect(applyCtrlHold('?')).toBe('\x7f')
  })

  it('escape sequences (mouse/focus tracking, arrows) pass through and STAY armed', () => {
    setCtrlHold(true)
    expect(applyCtrlHold('\x1b[I')).toBe('\x1b[I') // focus-in from clicking the terminal
    expect(applyCtrlHold('\x1b[<0;42;17M')).toBe('\x1b[<0;42;17M') // SGR mouse click
    expect(isCtrlHeld()).toBe(true)
    expect(applyCtrlHold('c')).toBe('\x03') // the actual keystroke still consumes
  })

  it('plain multi-char chunks (paste) pass through and disarm', () => {
    setCtrlHold(true)
    expect(applyCtrlHold('hello')).toBe('hello')
    expect(isCtrlHeld()).toBe(false)
  })
})

describe('TerminalControlBar — Ctrl in the ⋯ popover', () => {
  it('Ctrl is the first key in the popover; arming closes it and shows the bar chip', () => {
    render(<TerminalControlBar send={() => {}} />)
    expect(screen.queryByText('Ctrl')).toBeNull()
    fireEvent.click(screen.getByTitle('More keys'))
    const keys = screen.getAllByRole('button').map((b) => b.textContent)
    expect(keys.indexOf('Ctrl')).toBeLessThan(keys.indexOf('Home'))
    fireEvent.click(screen.getByText('Ctrl'))
    expect(isCtrlHeld()).toBe(true)
    // popover closed, armed chip remains in the main row; tapping it disarms
    fireEvent.click(screen.getByText('Ctrl'))
    expect(isCtrlHeld()).toBe(false)
    expect(screen.queryByText('Ctrl')).toBeNull()
  })
})
