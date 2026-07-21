import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import ChatTargetBanner from '@/components/chat/ChatTargetBanner'
import type { TargetMismatch } from '@/store/chatStore'

// ─── ChatTargetBanner — pin-vs-current target mismatch notice ───────────────
// Dismissal is sticky per (chat, resolved target): the same mismatch never
// re-shows, a NEW resolved target shows once again, and dismissal only hides
// the banner (the kebab row is the permanent home — its own test file).

const MISMATCH: TargetMismatch = {
  pinnedTarget: 'local',
  pinnedLabel: 'local sandbox',
  resolvedTarget: 'm-attic',
  resolvedLabel: 'Attic PC',
}

function renderBanner(over: Partial<Parameters<typeof ChatTargetBanner>[0]> = {}) {
  return render(
    <ChatTargetBanner
      chatId="c1"
      mismatch={MISMATCH}
      moveDisabled={false}
      onMove={() => {}}
      {...over}
    />,
  )
}

beforeEach(() => localStorage.clear())

describe('ChatTargetBanner', () => {
  it('local-pinned mismatch shows the "still runs on the local sandbox" copy', () => {
    renderBanner()
    expect(
      screen.getByText('This chat still runs on the local sandbox — new chats run on Attic PC.'),
    ).toBeTruthy()
    expect(screen.getByText('Move this chat to Attic PC')).toBeTruthy()
  })

  it('machine-pinned mismatch names the pinned machine', () => {
    renderBanner({ mismatch: { ...MISMATCH, pinnedTarget: 'm-old', pinnedLabel: 'Old box' } })
    expect(
      screen.getByText('This chat runs on Old box — new chats run on Attic PC.'),
    ).toBeTruthy()
  })

  it('renders nothing when there is no mismatch', () => {
    const { container } = renderBanner({ mismatch: null })
    expect(container.firstChild).toBeNull()
  })

  it('X records the resolved target under oto.chattarget.dismissed.<chatId> and hides', () => {
    renderBanner()
    fireEvent.click(screen.getByTitle('Dismiss'))
    expect(screen.queryByText(/new chats run on/)).toBeNull()
    expect(JSON.parse(localStorage.getItem('oto.chattarget.dismissed.c1')!)).toEqual(['m-attic'])
  })

  it('dismissal is sticky per resolved target — a NEW target shows once again', () => {
    const { rerender } = renderBanner()
    fireEvent.click(screen.getByTitle('Dismiss'))
    // Same resolved target on a re-render stays hidden.
    rerender(
      <ChatTargetBanner chatId="c1" mismatch={{ ...MISMATCH }} moveDisabled={false} onMove={() => {}} />,
    )
    expect(screen.queryByText(/new chats run on/)).toBeNull()
    // The agent moved again — a different resolved target re-shows.
    rerender(
      <ChatTargetBanner
        chatId="c1"
        mismatch={{ ...MISMATCH, resolvedTarget: 'm-basement', resolvedLabel: 'Basement PC' }}
        moveDisabled={false}
        onMove={() => {}}
      />,
    )
    expect(screen.getByText(/new chats run on Basement PC/)).toBeTruthy()
  })

  it('a fresh mount stays hidden for a previously dismissed target', () => {
    renderBanner()
    fireEvent.click(screen.getByTitle('Dismiss'))
    renderBanner()
    expect(screen.queryByText(/new chats run on/)).toBeNull()
  })

  it('the move button is disabled while the chat streams/warms', () => {
    renderBanner({ moveDisabled: true })
    const btn = screen.getByText('Move this chat to Attic PC') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it('the move button opens the confirm; "Move chat" fires onMove and closes it', () => {
    const onMove = vi.fn()
    renderBanner({ onMove })
    fireEvent.click(screen.getByText('Move this chat to Attic PC'))
    expect(screen.getByText('Move chat to Attic PC?')).toBeTruthy()
    fireEvent.click(screen.getByText('Move chat'))
    expect(onMove).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Move chat to Attic PC?')).toBeNull()
  })

  it('Cancel closes the confirm without firing the op', () => {
    const onMove = vi.fn()
    renderBanner({ onMove })
    fireEvent.click(screen.getByText('Move this chat to Attic PC'))
    fireEvent.click(screen.getByText('Cancel'))
    expect(onMove).not.toHaveBeenCalled()
    expect(screen.queryByText('Move chat to Attic PC?')).toBeNull()
  })
})
