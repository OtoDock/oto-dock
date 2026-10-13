/**
 * A crash in one page shows that page's card with a crash report to copy,
 * keeps the layout around it, and clears when the person leaves the page.
 * A path change never remounts a page that did not crash (a chat switch
 * keeps the chat page's socket and state).
 */
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes, useNavigate } from 'react-router-dom'
import { useEffect } from 'react'

import ErrorBoundary, { copyText, crashReport } from '@/components/ErrorBoundary'
import RouteBoundary, { PageBoundary } from '@/components/RouteBoundary'

const mounts = vi.hoisted(() => ({ count: 0 }))

function Boom(): never {
  throw new Error('the page broke')
}

// The layout route's outlet, as Layout.tsx renders it.
function RoutesOutlet() {
  return <Outlet />
}

function Healthy({ label }: { label: string }) {
  useEffect(() => { mounts.count += 1 }, [])
  return <div>{label}</div>
}

let go: (to: string) => void = () => {}
function Nav() {
  const navigate = useNavigate()
  go = navigate
  return null
}

beforeEach(() => {
  mounts.count = 0
  vi.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => vi.restoreAllMocks())

describe('the route error boundary', () => {
  it('shows the page card inside the layout and clears it on leaving the page', () => {
    render(
      <MemoryRouter initialEntries={['/admin/broken']}>
        <Nav />
        <Routes>
          <Route path="/admin" element={<div><nav>Admin sidebar</nav><PageBoundary><RoutesOutlet /></PageBoundary></div>}>
            <Route path="broken" element={<Boom />} />
            <Route path="users" element={<Healthy label="Users page" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('This page hit an error')).toBeTruthy()
    expect(screen.getByText('the page broke')).toBeTruthy()
    // The layout around the page is alive.
    expect(screen.getByText('Admin sidebar')).toBeTruthy()
    act(() => go('/admin/users'))
    expect(screen.queryByText('This page hit an error')).toBeNull()
    expect(screen.getByText('Users page')).toBeTruthy()
  })

  it('never remounts a healthy page when the path changes', () => {
    render(
      <MemoryRouter initialEntries={['/chat/a/1']}>
        <Nav />
        <Routes>
          <Route element={<RouteBoundary />}>
            <Route path="/chat/:name/:chatId" element={<Healthy label="Chat page" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )
    expect(mounts.count).toBe(1)
    act(() => go('/chat/a/2'))
    expect(screen.getByText('Chat page')).toBeTruthy()
    expect(mounts.count).toBe(1)
  })

  it('writes a report with the page, the build, the error and both stacks', () => {
    const err = new Error('bad block')
    err.stack = 'Error: bad block\n    at Block (Block.tsx:12:3)'
    const text = crashReport({
      error: err, componentStack: '\n    at Block\n    at ChatPage', at: '2026-10-09T00:00:00.000Z',
      path: '/admin/users?tab=x', build: 'b-123', userAgent: 'Test/1.0',
    })
    expect(text).toContain('Page: /admin/users?tab=x')
    expect(text).toContain('Build: b-123')
    expect(text).toContain('Error: Error: bad block')
    expect(text).toContain('at Block (Block.tsx:12:3)')
    expect(text).toContain('Component stack:\nat Block')
    expect(crashReport({ error: 'a string', componentStack: '', at: '', path: '/', build: '', userAgent: '' }))
      .toContain('Build: unknown')
  })

  it('copies the report, falling back to the copy command on a plain-http page', async () => {
    const secure = Object.getOwnPropertyDescriptor(window, 'isSecureContext')
    const execBefore = Object.getOwnPropertyDescriptor(document, 'execCommand')
    onTestFinished(() => {
      if (secure) Object.defineProperty(window, 'isSecureContext', secure)
      else delete (window as { isSecureContext?: boolean }).isSecureContext
      if (execBefore) Object.defineProperty(document, 'execCommand', execBefore)
      else delete (document as { execCommand?: unknown }).execCommand
    })
    Object.defineProperty(window, 'isSecureContext', { value: false, configurable: true })
    const exec = vi.fn(() => true)
    Object.defineProperty(document, 'execCommand', { value: exec, configurable: true })
    expect(await copyText('report')).toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
    render(<MemoryRouter><ErrorBoundary variant="route" resetKey="/x"><Boom /></ErrorBoundary></MemoryRouter>)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy crash report' })) })
    expect(screen.getByText('Copied')).toBeTruthy()
    exec.mockReturnValue(false)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy crash report' })) })
    expect(screen.getByText(/select the report below/)).toBeTruthy()
    expect((screen.getByLabelText('Crash report') as HTMLTextAreaElement).value).toContain('the page broke')
  })
})
