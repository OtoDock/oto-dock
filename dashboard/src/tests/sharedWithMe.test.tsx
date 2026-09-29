import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'

const mine = vi.hoisted(() => ({ current: [] as unknown[] }))
vi.mock('@/api/shares', () => ({
  useMyShares: () => ({ data: mine.current }),
}))

import SharedWithMe from '@/components/sharing/SharedWithMe'

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname}</div>
}

function renderStrip() {
  return render(
    <MemoryRouter initialEntries={['/agents']}>
      <SharedWithMe />
      <LocationProbe />
    </MemoryRouter>,
  )
}

describe('SharedWithMe', () => {
  it('renders nothing without shares and nothing for hidden ones', () => {
    mine.current = []
    const { container, unmount } = renderStrip()
    expect(container.textContent).toBe('/agents')
    unmount()
    mine.current = [{ id: 's1', target_kind: 'app', target_id: 'a', title: 'Ops', agent: 'dev', shared_by: 'x', shared_by_name: 'Nora', created_at: '', expires_at: null, hidden: true, href: '/apps/a' }]
    const r2 = renderStrip()
    expect(r2.container.textContent).toBe('/agents')
  })

  it('opens the page each share names', () => {
    mine.current = [
      { id: 's1', target_kind: 'app', target_id: 'a', title: 'Ops', agent: 'dev', shared_by: 'x', shared_by_name: 'Nora', created_at: '', expires_at: null, hidden: false, href: '/apps/a' },
    ]
    renderStrip()
    expect(screen.getByText('Shared with me')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Ops/ }))
    expect(screen.getByTestId('loc').textContent).toBe('/apps/a')
  })
})
