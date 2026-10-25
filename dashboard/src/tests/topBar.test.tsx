import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// ─── The chat top bar: every control on one 36 px height and one glass
//     recipe, the status dot inside the agent pill (after the name, before
//     the chevron), a tap on the dot shows its detail instead of opening the
//     agents page, and a long name truncates without pushing the dot out ───

import TopBar from '@/components/chat/TopBar'
import NotificationBell from '@/components/chat/notifications/NotificationBell'
import { GLASS } from '@/components/ui/glass'

const user = { sub: 'u1', name: 'dev-admin', display_name: 'Dev Admin', email: 'a@x', role: 'admin' } as never

function renderBar(over: { name?: string; bell?: boolean } = {}) {
  render(
    <MemoryRouter initialEntries={['/chat/alpha']}>
      <Routes>
        <Route
          path="/chat/alpha"
          element={(
            <TopBar
              agentName="alpha"
              displayName={over.name ?? 'Alpha Prime'}
              onToggleHistory={() => {}}
              user={user}
              onLogout={() => {}}
              notificationBell={over.bell ? <NotificationBell unreadCount={3} onClick={() => {}} panelOpen={false} /> : undefined}
              machineStatus="online"
              machineName="laptop"
              machineScope="admin"
            />
          )}
        />
        <Route path="/agents" element={<p>Agents page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

const pill = () => screen.getByTestId('agent-pill')

describe('TopBar', () => {
  it('puts every control on the 36 px height', () => {
    renderBar({ bell: true })
    for (const b of [
      screen.getByTitle('Toggle chat history'),
      screen.getByRole('button', { name: /^Notifications/ }),
      screen.getByTitle('Dev Admin'),
    ]) {
      expect(b.className.split(/\s+/)).toContain('size-control-md')
    }
    expect(pill().className.split(/\s+/)).toContain('h-control-md')
  })

  it('paints the glass from the shared recipe, the pill on a span behind its content', () => {
    renderBar({ bell: true })
    const recipe = GLASS.split(' ')
    const history = screen.getByTitle('Toggle chat history').className.split(/\s+/)
    for (const c of recipe) expect(history).toContain(c)
    // The pill itself carries no blur: its fixed tooltip would be placed
    // against the pill instead of the viewport.
    expect(pill().className).not.toMatch(/backdrop-blur/)
    const glass = pill().querySelector('span[aria-hidden="true"]') as HTMLElement
    for (const c of recipe) expect(glass.className.split(/\s+/)).toContain(c)
    // The pill lights where a click navigates (the link), not over the dot.
    expect(glass.className.split(/\s+/)).toContain('group-has-[a:hover]:bg-glass-hover')
    expect(glass.className).not.toMatch(/(^|\s)group-hover:/)
  })

  it('shows the status dot inside the pill, after the name and before the chevron', () => {
    renderBar()
    const link = within(pill()).getByRole('link', { name: 'Alpha Prime' })
    const dot = within(pill()).getByRole('button', { name: /laptop: Connected/ })
    const chevron = pill().querySelector('svg:last-of-type') as SVGElement
    expect(link.compareDocumentPosition(dot) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(dot.compareDocumentPosition(chevron) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The dot is not inside the link: no control nests in an anchor.
    expect(link.contains(dot)).toBe(false)
  })

  it('a tap on the dot shows its detail and stays on the chat', () => {
    renderBar()
    fireEvent.click(within(pill()).getByRole('button', { name: /laptop: Connected/ }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('laptop: Connected')
    expect(screen.queryByText('Agents page')).toBeNull()
  })

  it('a long name truncates on the link and keeps its full text as the title', () => {
    const long = 'A very long agent name that cannot fit on a phone at all'
    renderBar({ name: long })
    const link = within(pill()).getByRole('link', { name: long })
    expect(link.className.split(/\s+/)).toEqual(expect.arrayContaining(['min-w-0', 'truncate']))
    expect(link).toHaveAttribute('title', long)
    expect(pill().className.split(/\s+/)).toContain('max-w-[45vw]')
  })

  it('the avatar button says whether its menu is open', () => {
    renderBar()
    const avatar = screen.getByTitle('Dev Admin')
    expect(avatar).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(avatar)
    expect(avatar).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('User Settings')).toBeInTheDocument()
  })
})
