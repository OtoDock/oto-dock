import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

// The plan review's feedback box follows the composer's key rule: plain
// Enter adds a line, Shift/Ctrl/Cmd+Enter sends on a fine pointer, only the
// Send Feedback button on a touch device.

import PlanReviewCard from '@/components/chat/PlanReviewCard'

function open(over: Record<string, unknown> = {}) {
  const onRespond = vi.fn()
  const onSendMessage = vi.fn()
  render(
    <PlanReviewCard requestId="r1" plan="# Plan" toolInput={{}} onRespond={onRespond} onSendMessage={onSendMessage} {...over} />,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Edit Plan' }))
  const box = screen.getByPlaceholderText('Describe what to change in the plan...')
  fireEvent.change(box, { target: { value: 'split step two' } })
  return { box, onRespond, onSendMessage }
}

afterEach(() => {
  delete (window as { matchMedia?: unknown }).matchMedia
})

describe('PlanReviewCard feedback keys', () => {
  it('plain Enter adds a line, Shift+Enter sends', () => {
    const { box, onSendMessage } = open()
    expect(fireEvent.keyDown(box, { key: 'Enter' })).toBe(true)
    expect(onSendMessage).not.toHaveBeenCalled()
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(onSendMessage).toHaveBeenCalledWith('Please modify the plan: split step two')
  })

  it('Ctrl+Enter and Cmd+Enter send too, an IME Enter does not', () => {
    const { box, onSendMessage } = open()
    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true, isComposing: true })
    expect(onSendMessage).not.toHaveBeenCalled()
    fireEvent.keyDown(box, { key: 'Enter', metaKey: true })
    expect(onSendMessage).toHaveBeenCalledTimes(1)
  })

  it('on a touch device only the button sends', () => {
    window.matchMedia = vi.fn().mockImplementation((q: string) => ({
      matches: q === '(pointer: coarse)', media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })) as unknown as typeof window.matchMedia
    const { box, onSendMessage } = open()
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(onSendMessage).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Send Feedback' }))
    expect(onSendMessage).toHaveBeenCalledTimes(1)
  })
})
