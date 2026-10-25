import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { useCallback, useState } from 'react'

// ─── The notifications panel and its bell: a press on the bell while the
//     panel is open closes it, and the bell's click that follows does not
//     open it again; a press anywhere else closes it ───

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: null }) }))

import NotificationPanel from '@/components/chat/notifications/NotificationPanel'
import NotificationBell from '@/components/chat/notifications/NotificationBell'
import { POPOVER } from '@/components/ui/classes'

const noop = () => {}

// The composition of hooks/useChatNotifications: the toggle reads the open
// state of its render.
function BellAndPanel() {
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])
  const toggle = () => (open ? close() : setOpen(true))
  return (
    <>
      <p>Elsewhere</p>
      <div className="relative">
        <NotificationBell unreadCount={0} onClick={toggle} panelOpen={open} />
        {open && (
          <NotificationPanel deliveries={[]} loading={false}
            onMarkRead={noop} onMarkAllRead={noop} onDismiss={noop} onAcknowledge={noop} onClose={close} />
        )}
      </div>
    </>
  )
}

function press(el: Element) {
  fireEvent.mouseDown(el)
  fireEvent.mouseUp(el)
  fireEvent.click(el)
}

describe('NotificationPanel and the bell', () => {
  it('the bell opens the panel and a second press closes it', () => {
    render(<BellAndPanel />)
    const bell = screen.getByRole('button', { name: 'Notifications' })
    press(bell)
    expect(screen.getByText('No notifications')).toBeInTheDocument()
    expect(bell).toHaveAttribute('aria-expanded', 'true')
    press(bell)
    expect(screen.queryByText('No notifications')).toBeNull()
    expect(bell).toHaveAttribute('aria-expanded', 'false')
  })

  it('a press outside closes the panel', () => {
    render(<BellAndPanel />)
    press(screen.getByRole('button', { name: 'Notifications' }))
    press(screen.getByText('Elsewhere'))
    expect(screen.queryByText('No notifications')).toBeNull()
  })

  it('sits on the overlay recipe in the body, above the composer and below the toasts', () => {
    render(<BellAndPanel />)
    press(screen.getByRole('button', { name: 'Notifications' }))
    const panel = screen.getByRole('dialog', { name: 'Notifications' })
    for (const c of POPOVER.split(' ')) expect(panel.className.split(/\s+/)).toContain(c)
    expect(panel.className.split(/\s+/)).toContain('z-[45]')
    expect(panel.parentElement).toBe(document.body)
    // Not a second landmark beside the toast region of the same name, and
    // not modal: the page behind stays usable.
    expect(panel).not.toHaveAttribute('aria-modal')
    expect(screen.queryAllByRole('region', { name: 'Notifications' })).toHaveLength(0)
  })

  it('on a desktop the focus moves into the panel, and Escape gives it back to the bell', () => {
    render(<BellAndPanel />)
    const bell = screen.getByRole('button', { name: 'Notifications' })
    bell.focus()
    press(bell)
    const panel = screen.getByRole('dialog', { name: 'Notifications' })
    expect(panel).toHaveFocus()
    fireEvent.keyDown(panel, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Notifications' })).toBeNull()
    expect(bell).toHaveFocus()
  })

  it('the scrim closes the panel on its click, not on the press', () => {
    render(<BellAndPanel />)
    press(screen.getByRole('button', { name: 'Notifications' }))
    const scrim = document.querySelector('[data-notification-scrim]') as HTMLElement
    fireEvent.mouseDown(scrim)
    expect(screen.getByText('No notifications')).toBeInTheDocument()
    fireEvent.click(scrim)
    expect(screen.queryByText('No notifications')).toBeNull()
  })

  // In dark the card is two surface steps under the overlay: a read row (no
  // fill of its own) on the card would be a dark band inside the panel.
  it('a read row sits on the panel\'s own surface, not the card under it', () => {
    const read = {
      id: 'd1', notification_id: null, title: 'Already seen', body: '', severity: 'info' as const,
      scope: 'user', source: 'test', delivered_at: new Date().toISOString(), read: 1, dismissed: 0,
      read_at: null, dismissed_at: null, agent_slug: null, chat_id: null,
    }
    render(
      <NotificationPanel deliveries={[read]} loading={false}
        onMarkRead={noop} onMarkAllRead={noop} onDismiss={noop} onAcknowledge={noop} onClose={noop} />,
    )
    let row: HTMLElement | null = screen.getByText('Already seen')
    // The first ancestor with a fill of its own (a hover does not count).
    const fill = (el: HTMLElement) => el.className.split(/\s+/).some((c) => c.startsWith('bg-surface-'))
    while (row && !fill(row)) row = row.parentElement
    expect(row?.className.split(/\s+/)).toContain('bg-surface-overlay')
    expect(POPOVER.split(' ')).toContain('bg-surface-overlay')
  })

  it('opens under the bell wherever the bar sits (a banner above pushes it down)', () => {
    render(<BellAndPanel />)
    const bell = screen.getByRole('button', { name: 'Notifications' })
    bell.getBoundingClientRect = () => ({ top: 84, bottom: 120, left: 300, right: 336, width: 36, height: 36, x: 300, y: 84, toJSON: () => ({}) })
    press(bell)
    expect(screen.getByRole('dialog', { name: 'Notifications' }).style.top).toBe('126px')
  })

  it('without a bell on the page the class places it', () => {
    render(
      <NotificationPanel deliveries={[]} loading={false}
        onMarkRead={noop} onMarkAllRead={noop} onDismiss={noop} onAcknowledge={noop} onClose={noop} />,
    )
    const panel = screen.getByRole('dialog', { name: 'Notifications' })
    expect(panel.style.top).toBe('')
    expect(panel.className.split(/\s+/)).toContain('top-12')
  })
})
