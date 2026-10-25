// The workspace overlay's "new message" dot: the assistant id is noted when
// the overlay opens, the dot lights when a newer id arrives while it is
// open, and it clears on close; selection mode ends when the overlay closes.
import { describe, it, expect } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useWorkspaceState } from '@/hooks/useWorkspaceState'

const wrapper = ({ children }: { children: ReactNode }) => (
  <MemoryRouter initialEntries={['/chat/a']}>{children}</MemoryRouter>
)

function mount(agent = 'a') {
  return renderHook(
    ({ last }: { last: string | null }) => useWorkspaceState(agent, last),
    { wrapper, initialProps: { last: 'm1' } },
  )
}

describe('useWorkspaceState', () => {
  it('lights the dot for an assistant message newer than the one seen at open', () => {
    const h = mount()
    expect(h.result.current.state.open).toBe(false)
    expect(h.result.current.state.hasNewMessage).toBe(false)
    act(() => h.result.current.toggleWorkspace())
    expect(h.result.current.state.open).toBe(true)
    expect(h.result.current.state.hasNewMessage).toBe(false)
    h.rerender({ last: 'm2' })
    expect(h.result.current.state.hasNewMessage).toBe(true)
    act(() => h.result.current.closeWorkspace())
    expect(h.result.current.state.hasNewMessage).toBe(false)
    act(() => h.result.current.openWorkspace())
    expect(h.result.current.state.hasNewMessage).toBe(false)
  })

  it('leaves selection mode when the overlay closes', () => {
    const h = mount()
    act(() => h.result.current.openWorkspace())
    act(() => h.result.current.enterSelectionMode('a.txt'))
    expect(h.result.current.state.selectionMode).toBe(true)
    expect(h.result.current.state.selected).toEqual(['a.txt'])
    act(() => h.result.current.closeWorkspace())
    expect(h.result.current.state.selectionMode).toBe(false)
  })
})
