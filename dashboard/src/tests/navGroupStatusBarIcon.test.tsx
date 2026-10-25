// NavGroup opens when a child becomes the route and stays closed once the
// person closed it on that route; the status bar's timer reads 0 while idle
// and counts while streaming; an icon that failed retries for a new name.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import NavGroup from '@/components/ui/NavGroup'
import ChatStatusBar from '@/components/chat/ChatStatusBar'
import McpIcon from '@/components/McpIcon'

const items = [
  { label: 'Users', path: '/admin/users' },
  { label: 'Shares', path: '/admin/shares' },
]

function group(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <NavGroup label="Admin" items={items} onNavigate={() => {}} />
    </MemoryRouter>,
  )
}

describe('NavGroup', () => {
  it('starts open on an active child and closed elsewhere', () => {
    const a = group('/admin/users')
    expect(screen.getByText('Users')).toBeInTheDocument()
    a.unmount()
    group('/other')
    expect(screen.queryByText('Users')).toBeNull()
  })

  it('stays closed after a manual collapse on the active route', () => {
    group('/admin/users')
    fireEvent.click(screen.getByText('Admin'))
    expect(screen.queryByText('Users')).toBeNull()
  })
})

describe('ChatStatusBar elapsed', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  function bar(over: Partial<Parameters<typeof ChatStatusBar>[0]>) {
    return render(
      <ChatStatusBar
        streaming={false} warming={false} startTime={null} thinkingActive={false}
        compressingActive={false} activeAgents={[]} mode="ask" model="m" costUsd={0}
        contextUsed={0} contextMax={0} onModeChange={() => {}} onModelChange={() => {}}
        {...over}
      />,
    )
  }

  it('counts while streaming and reads 0 again when the turn ends', () => {
    const now = Date.now()
    const r = bar({ streaming: true, startTime: now })
    act(() => { vi.advanceTimersByTime(2100) })
    expect(screen.getByText('2s')).toBeInTheDocument()
    r.rerender(
      <ChatStatusBar
        streaming={false} warming={false} startTime={null} thinkingActive={false}
        compressingActive={false} activeAgents={[]} mode="ask" model="m" costUsd={0}
        contextUsed={0} contextMax={0} onModeChange={() => {}} onModelChange={() => {}}
      />,
    )
    expect(screen.queryByText('2s')).toBeNull()
  })
})

describe('McpIcon', () => {
  it('retries the image for a new name after a failure', () => {
    const { container, rerender } = render(<McpIcon name="a-mcp" label="A" category="community" author="X" hasIcon />)
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('img')).toBeNull()
    rerender(<McpIcon name="b-mcp" label="B" category="community" author="X" hasIcon />)
    expect(container.querySelector('img')?.getAttribute('src')).toBe('/v1/mcps/b-mcp/icon.png')
  })
})
