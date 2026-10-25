// The delayed mount and the fallback banner keep their timing: true only
// after the delay has fully elapsed, back to false the moment the condition
// drops; the banner shows for its window and shows again for a new reason.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, renderHook, screen } from '@testing-library/react'
import { useDelayMount } from '@/hooks/useDelayMount'
import RemoteFallbackBanner from '@/components/chat/RemoteFallbackBanner'

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('useDelayMount', () => {
  it('turns true only after the delay and resets when the condition drops', () => {
    const h = renderHook(({ on }: { on: boolean }) => useDelayMount(on, 500), { initialProps: { on: true } })
    expect(h.result.current).toBe(false)
    act(() => { vi.advanceTimersByTime(499) })
    expect(h.result.current).toBe(false)
    act(() => { vi.advanceTimersByTime(1) })
    expect(h.result.current).toBe(true)
    h.rerender({ on: false })
    expect(h.result.current).toBe(false)
    h.rerender({ on: true })
    expect(h.result.current).toBe(false)
    act(() => { vi.advanceTimersByTime(500) })
    expect(h.result.current).toBe(true)
  })

  it('a condition that drops before the delay never mounts', () => {
    const h = renderHook(({ on }: { on: boolean }) => useDelayMount(on, 500), { initialProps: { on: true } })
    act(() => { vi.advanceTimersByTime(300) })
    h.rerender({ on: false })
    act(() => { vi.advanceTimersByTime(500) })
    expect(h.result.current).toBe(false)
  })
})

describe('RemoteFallbackBanner', () => {
  it('shows for the window, hides, and shows again for the next override reason', () => {
    const { rerender } = render(<RemoteFallbackBanner fallbackReason="user-override-offline" machineName="lab" durationMs={1000} />)
    expect(screen.getByText(/lab/)).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(1000) })
    expect(screen.queryByText(/lab/)).toBeNull()
    rerender(<RemoteFallbackBanner fallbackReason={null} machineName="lab" durationMs={1000} />)
    rerender(<RemoteFallbackBanner fallbackReason="user-override-offline" machineName="lab" durationMs={1000} />)
    expect(screen.getByText(/lab/)).toBeInTheDocument()
  })

  it('shows nothing for another reason', () => {
    render(<RemoteFallbackBanner fallbackReason="agent-default-offline" machineName="lab" durationMs={1000} />)
    expect(screen.queryByText(/lab/)).toBeNull()
  })
})
