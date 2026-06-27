// Apps-UI open/close rules (operator, 2026-07-12) — the navigation-driven
// auto-open/auto-close decision extracted from AgentChat:
//   (a) arriving on an agent HOME with pinned apps → open (every arrival,
//       including agent switches and chat→home);
//   (b) entering any chat → close, never auto-open (deep links included);
//   (c) a pins refetch after a manual close must NOT re-open.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useAppsAutoOpen } from '../hooks/useAppsAutoOpen'

type Props = {
  agent: string | undefined
  chat: string | undefined
  pins: unknown[] | undefined
}

const PINS = [{ id: 'a1' }]

describe('useAppsAutoOpen', () => {
  let setOpen: ReturnType<typeof vi.fn<(open: boolean) => void>>

  beforeEach(() => {
    setOpen = vi.fn<(open: boolean) => void>()
  })

  function mount(initial: Props) {
    return renderHook(
      ({ agent, chat, pins }: Props) =>
        useAppsAutoOpen(agent, chat, pins, setOpen),
      { initialProps: initial },
    )
  }

  it('opens on first landing on a home page with pins', () => {
    mount({ agent: 'helper', chat: undefined, pins: PINS })
    expect(setOpen).toHaveBeenCalledWith(true)
  })

  it('waits for the async pins load, then opens exactly once', () => {
    const h = mount({ agent: 'helper', chat: undefined, pins: undefined })
    expect(setOpen).not.toHaveBeenCalled()
    h.rerender({ agent: 'helper', chat: undefined, pins: PINS })
    expect(setOpen).toHaveBeenCalledTimes(1)
    expect(setOpen).toHaveBeenCalledWith(true)
    // A later pins refetch (new array identity) must not re-open — the
    // arrival was already consumed (manual-close protection).
    h.rerender({ agent: 'helper', chat: undefined, pins: [...PINS] })
    expect(setOpen).toHaveBeenCalledTimes(1)
  })

  it('re-opens on an agent SWITCH onto a pinned home', () => {
    const h = mount({ agent: 'a', chat: undefined, pins: PINS })
    setOpen.mockClear()
    h.rerender({ agent: 'b', chat: undefined, pins: PINS })
    expect(setOpen).toHaveBeenCalledWith(true)
  })

  it('opens again when returning from a chat to home', () => {
    const h = mount({ agent: 'a', chat: undefined, pins: PINS })
    h.rerender({ agent: 'a', chat: 'c-1', pins: PINS })
    setOpen.mockClear()
    h.rerender({ agent: 'a', chat: undefined, pins: PINS })
    expect(setOpen).toHaveBeenCalledWith(true)
  })

  it('closes when entering a chat (Active-now click / any navigation)', () => {
    const h = mount({ agent: 'a', chat: undefined, pins: PINS })
    setOpen.mockClear()
    h.rerender({ agent: 'a', chat: 'c-1', pins: PINS })
    expect(setOpen).toHaveBeenCalledWith(false)
    expect(setOpen).not.toHaveBeenCalledWith(true)
  })

  it('never opens on a deep link straight into a chat', () => {
    mount({ agent: 'a', chat: 'c-9', pins: PINS })
    expect(setOpen).not.toHaveBeenCalledWith(true)
  })

  it('stays closed on a pinless home', () => {
    const h = mount({ agent: 'a', chat: undefined, pins: [] })
    expect(setOpen).not.toHaveBeenCalled()
    // pins appearing later on the SAME view do not open by themselves —
    // the arrival was consumed by the empty load.
    h.rerender({ agent: 'a', chat: undefined, pins: PINS })
    expect(setOpen).not.toHaveBeenCalled()
  })
})
