// Apps-UI open/close rules (operator, 2026-07-12 + 2026-08-15) — the
// navigation-driven auto-open/auto-close decision extracted from AgentChat:
//   (a) arriving on an agent HOME with pinned apps → open (every arrival,
//       including agent switches and chat→home);
//   (b) entering any chat → close, never auto-open (deep links included);
//   (c) a pins refetch after a manual close must NOT re-open;
//   (d) a PHONE-MODE chat entry (wake / mic-start mint, keep-ref armed) →
//       the close consumes the keep and the panel stays/opens instead. The
//       keep is consumed at the close site because the URL rewrite lands in
//       a later react-router transition commit than the internal chat state
//       — an effect-side re-open one-shot loses that race (live-hit
//       2026-08-14, the failed first fix).
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useEffect, useRef, type MutableRefObject } from 'react'
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

  function mountWithKeep(initial: Props, keep: MutableRefObject<boolean>) {
    return renderHook(
      ({ agent, chat, pins }: Props) =>
        useAppsAutoOpen(agent, chat, pins, setOpen, keep),
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

  // ---- phone-mode keep (the 5th param) ----

  it('a phone-mode chat entry keeps the panel: consume ref, open instead of close', () => {
    const keep = { current: false }
    const h = mountWithKeep({ agent: 'a', chat: undefined, pins: PINS }, keep)
    setOpen.mockClear()
    // The mint set internal state in an earlier commit; the ref is armed
    // BETWEEN commits — the URL lands here, in the transition commit.
    keep.current = true
    h.rerender({ agent: 'a', chat: 'c-2', pins: PINS })
    expect(setOpen).toHaveBeenCalledWith(true)
    expect(setOpen).not.toHaveBeenCalledWith(false)
    expect(keep.current).toBe(false) // consumed exactly once
  })

  it('the keep is one-shot: the NEXT chat entry closes normally', () => {
    const keep = { current: true }
    const h = mountWithKeep({ agent: 'a', chat: undefined, pins: PINS }, keep)
    h.rerender({ agent: 'a', chat: 'c-1', pins: PINS })
    setOpen.mockClear()
    h.rerender({ agent: 'a', chat: 'c-2', pins: PINS })
    expect(setOpen).toHaveBeenCalledWith(false)
    expect(setOpen).not.toHaveBeenCalledWith(true)
  })

  it('deep-link mount with the keep armed opens once pins land late', () => {
    // Component-shaped harness: the AgentChat pattern arms the ref DURING
    // RENDER (from ?wake=1) — the only writer guaranteed to precede this
    // hook's effect on a fresh mount. An effect-armed ref would be too
    // late; this harness pins the contract.
    function Harness({ agent, chat, pins }: Props) {
      const keep = useRef(false)
      keep.current = keep.current || true // render-time arm (?wake=1)
      useAppsAutoOpen(agent, chat, pins, setOpen, keep)
      return null
    }
    const h = renderHook(Harness, {
      initialProps: { agent: 'a', chat: 'c-9', pins: undefined as unknown[] | undefined },
    })
    expect(setOpen).not.toHaveBeenCalled() // pins not known yet — no flicker
    h.rerender({ agent: 'a', chat: 'c-9', pins: PINS })
    expect(setOpen).toHaveBeenCalledWith(true)
  })

  it('an effect-armed ref on a fresh chat mount is provably too late (the trap)', () => {
    // Documents WHY AgentChat arms at render time: an arm in a component
    // effect runs after the hook's mount close — the keep is never seen.
    function Harness({ agent, chat, pins }: Props) {
      const keep = useRef(false)
      useAppsAutoOpen(agent, chat, pins, setOpen, keep)
      useEffect(() => { keep.current = true }, [])
      return null
    }
    const h = renderHook(Harness, {
      initialProps: { agent: 'a', chat: 'c-9', pins: PINS },
    })
    h.rerender({ agent: 'a', chat: 'c-9', pins: PINS })
    expect(setOpen).not.toHaveBeenCalledWith(true)
  })

  it('a kept entry with NO pins opens nothing', () => {
    const keep = { current: false }
    const h = mountWithKeep({ agent: 'a', chat: undefined, pins: [] }, keep)
    keep.current = true
    h.rerender({ agent: 'a', chat: 'c-1', pins: [] })
    expect(setOpen).not.toHaveBeenCalledWith(true)
  })
})
