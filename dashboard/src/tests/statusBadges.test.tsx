import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Link, Route, Routes } from 'react-router-dom'

// ─── RemoteBadge: the status dot's detail on the shared Tooltip ────────────
// A tap focuses the dot and the focus shows the detail; the click never
// toggles it shut, never navigates the link the dot sits in (an agent card)
// and never opens a clickable row around it. A dot that kept the focus after
// its detail closed (a scroll, an outside touch) shows it again on a tap.

import RemoteBadge from '@/components/RemoteBadge'
import StatusBadge from '@/components/StatusBadge'

const dot = () => screen.getByRole('button', { name: /laptop: Connected/ })

describe('RemoteBadge', () => {
  it('a tap inside a link shows the detail and stays on the page', () => {
    const rowClick = vi.fn()
    render(
      <MemoryRouter initialEntries={['/agents']}>
        <Routes>
          <Route path="/agents" element={
            <div onClick={rowClick}>
              <Link to="/chat/alpha">Alpha <RemoteBadge state="online" machineName="laptop" /></Link>
            </div>
          } />
          <Route path="/chat/alpha" element={<p>Chat page</p>} />
        </Routes>
      </MemoryRouter>,
    )
    // The click's default action, following the link, is prevented.
    const defaultRan = fireEvent.click(dot())
    expect(defaultRan).toBe(false)
    expect(screen.getByRole('tooltip')).toHaveTextContent('laptop: Connected')
    expect(screen.queryByText('Chat page')).toBeNull()
    expect(rowClick).not.toHaveBeenCalled()
  })

  it('a tap, the focus then the click, keeps the detail open', () => {
    render(<RemoteBadge state="online" machineName="laptop" />)
    fireEvent.focus(dot())
    expect(screen.getByRole('tooltip')).toBeTruthy()
    fireEvent.click(dot())
    expect(screen.getByRole('tooltip')).toHaveTextContent('laptop: Connected')
  })

  it('a tap after a scroll closed the detail shows it again', () => {
    render(<RemoteBadge state="online" machineName="laptop" />)
    fireEvent.click(dot())
    expect(screen.getByRole('tooltip')).toBeTruthy()
    fireEvent.scroll(window)
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(document.activeElement).toBe(dot())
    fireEvent.click(dot())
    expect(screen.getByRole('tooltip')).toHaveTextContent('laptop: Connected')
  })

  it('a touch outside the dot hides the detail', () => {
    render(
      <div>
        <RemoteBadge state="online" machineName="laptop" />
        <p>Elsewhere</p>
      </div>,
    )
    fireEvent.click(dot())
    expect(screen.getByRole('tooltip')).toBeTruthy()
    fireEvent.touchStart(screen.getByText('Elsewhere'))
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

// ─── StatusBadge: one tone per meaning ─────────────────────────────────────
// A queued run is not a cancelled one, and a live meeting is agent work.

describe('StatusBadge', () => {
  const pill = (status: string) => {
    const { unmount } = render(<StatusBadge status={status} />)
    const el = screen.getByText(status === 'pending' ? 'queued' : status)
    const cls = el.className
    unmount()
    return cls
  }

  it('reads a queued run apart from a cancelled one', () => {
    expect(pill('pending')).toContain('bg-status-info-bg')
    expect(pill('cancelled')).toContain('bg-surface-muted')
  })

  it('reads a live meeting on the agent accent with its pulsing dot', () => {
    const { container } = render(<StatusBadge status="active" />)
    expect(screen.getByText('active').className).toContain('bg-accent-agent-bg')
    expect(container.querySelector('.animate-pulse')!.className).toContain('bg-accent-agent-solid')
  })

  it('keeps a running task on the information tone', () => {
    const { container } = render(<StatusBadge status="running" />)
    expect(screen.getByText('running').className).toContain('bg-status-info-bg')
    expect(container.querySelector('.animate-pulse')!.className).toContain('bg-status-info-solid')
  })
})
