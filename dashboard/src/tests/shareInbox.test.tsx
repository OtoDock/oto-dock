import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

// "Shared with you" (SHARING.md): the block at the top of the notifications
// panel renders each item by the actions the server gave it. A decision
// item has Accept with a select of the person's agents and Decline, and no
// x; a received item has the x and Open. The panel passes it through by
// props, so it renders with no QueryClient and no router.

import NotificationPanel from '@/components/chat/notifications/NotificationPanel'
import ShareInboxSection from '@/components/sharing/ShareInboxSection'
import NotificationBell from '@/components/chat/notifications/NotificationBell'
import type { ShareInboxItem } from '@/api/shares'
import type { AgentSummary } from '@/api/agents'

const agents = [
  { name: 'alpha', display_name: 'Alpha Prime', color: '#336699' },
  { name: 'lite', display_name: 'Lite', color: '' },
] as unknown as AgentSummary[]

const mkItem = (over: Partial<ShareInboxItem>): ShareInboxItem => ({
  id: 's1', kind: 'app', target_id: 'app-1', title: 'Board', agent: 'alpha', agent_name: 'Alpha Prime',
  agent_color: '#336699', shared_by: 'u-nora', shared_by_name: 'Nora', created_at: '2026-10-04T00:00:00Z',
  expires_at: null, role_cap: 'viewer', href: '/apps/app-1', placed_agent: '',
  actions: ['accept', 'decline'], ...over,
})

const noop = () => {}

describe('ShareInboxSection', () => {
  it('a decision item offers Accept into one of my agents (the app\'s own left out) and Decline, no x', () => {
    const onAccept = vi.fn()
    const onDecline = vi.fn()
    render(<ShareInboxSection items={[mkItem({ role_cap: 'editor' })]} agents={agents}
      onAccept={onAccept} onDecline={onDecline} onHide={noop} onOpen={noop} />)
    expect(screen.getByText('Shared with you')).toBeTruthy()
    expect(screen.getByText('Board')).toBeTruthy()
    expect(screen.getByText(/from Nora · Alpha Prime · as editor/)).toBeTruthy()
    const select = screen.getByLabelText('Place in') as HTMLSelectElement
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['lite'])
    expect(screen.queryByTitle('Hide')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(onAccept).toHaveBeenCalledWith(expect.objectContaining({ id: 's1' }), 'lite')
    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))
    expect(onDecline).toHaveBeenCalledWith(expect.objectContaining({ id: 's1' }))
  })

  it('with no agent to place into, the item says so and keeps Decline', () => {
    render(<ShareInboxSection items={[mkItem({})]} agents={[agents[0]]}
      onAccept={noop} onDecline={noop} onHide={noop} onOpen={noop} />)
    expect(screen.queryByLabelText('Place in')).toBeNull()
    expect(screen.getByText(/Join an agent to place it/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Decline' })).toBeTruthy()
  })

  it('a received app or chat has the x (hide) and Open, and names where it was placed', () => {
    const onHide = vi.fn()
    const onOpen = vi.fn()
    render(<ShareInboxSection items={[
      mkItem({ id: 'a', actions: ['open', 'hide'], placed_agent: 'lite' }),
      mkItem({ id: 'c', kind: 'chat', title: 'Plan review', href: '/shared/c', actions: ['open', 'hide'] }),
    ]} agents={agents} onAccept={noop} onDecline={noop} onHide={onHide} onOpen={onOpen} />)
    expect(screen.getAllByTestId('share-inbox-received')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull()
    expect(screen.getByText(/in Lite/)).toBeTruthy()
    fireEvent.click(screen.getAllByTitle('Hide')[1])
    expect(onHide).toHaveBeenCalledWith(expect.objectContaining({ id: 'c' }))
    fireEvent.click(screen.getAllByTitle('Open')[0])
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'a' }))
  })
  it('a swiped item whose hide failed comes back beside the error', () => {
    vi.useFakeTimers()
    try {
      const onHide = vi.fn()
      const items = [mkItem({ id: 'a', actions: ['open', 'hide'] })]
      const props = { agents, onAccept: noop, onDecline: noop, onHide, onOpen: noop }
      const { rerender } = render(<ShareInboxSection items={items} {...props} />)
      const foreground = screen.getByTestId('share-inbox-received').parentElement as HTMLElement
      expect((foreground.parentElement as HTMLElement).style.maxHeight).toBe('')
      fireEvent.touchStart(foreground, { touches: [{ clientX: 300, clientY: 10 }] })
      fireEvent.touchMove(foreground, { touches: [{ clientX: 50, clientY: 10 }] })
      fireEvent.touchMove(foreground, { touches: [{ clientX: 50, clientY: 10 }] })
      fireEvent.touchEnd(foreground)
      vi.advanceTimersByTime(250)
      expect(onHide).toHaveBeenCalledWith(expect.objectContaining({ id: 'a' }))
      // The hide failed: the item is still in the list, the error names it.
      rerender(<ShareInboxSection items={items} error="Could not hide the share" {...props} />)
      vi.advanceTimersByTime(250)
      expect(screen.getByRole('alert').textContent).toBe('Could not hide the share')
      const row = screen.getByTestId('share-inbox-received').parentElement?.parentElement as HTMLElement
      expect(row.style.maxHeight).not.toBe('0px')
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('NotificationPanel — the shares block', () => {
  const panel = (over: { inbox?: ShareInboxItem[]; loading?: boolean }) => render(
    <NotificationPanel deliveries={[]} loading={over.loading ?? false} agents={agents}
      onMarkRead={noop} onMarkAllRead={noop} onDismiss={noop} onAcknowledge={noop} onClose={noop}
      inbox={over.inbox} onAcceptShare={noop} onDeclineShare={noop} onHideShare={noop} onOpenShare={noop} />,
  )

  it('shows the block above the groups, while the deliveries load too, and the empty state only without it', () => {
    const { unmount } = panel({ inbox: [mkItem({})], loading: true })
    expect(screen.getByTestId('share-inbox')).toBeTruthy()
    expect(screen.getByText('Loading...')).toBeTruthy()
    unmount()
    panel({ inbox: [mkItem({})] })
    expect(screen.queryByText('No notifications')).toBeNull()
    expect(screen.getByTestId('share-inbox-decision')).toBeTruthy()
  })

  it('reads "No notifications" with neither deliveries nor shares', () => {
    panel({ inbox: [] })
    expect(screen.getByText('No notifications')).toBeTruthy()
    expect(screen.queryByTestId('share-inbox')).toBeNull()
  })
})

describe('NotificationBell — the pending mark', () => {
  it('shows the decisions waiting as their own mark beside the unread number', () => {
    const { rerender } = render(<NotificationBell unreadCount={2} pendingCount={1} onClick={noop} panelOpen={false} />)
    expect(screen.getByText('2')).toBeTruthy()
    expect(screen.getByTestId('bell-pending').textContent).toBe('1')
    rerender(<NotificationBell unreadCount={0} pendingCount={0} onClick={noop} panelOpen={false} />)
    expect(screen.queryByTestId('bell-pending')).toBeNull()
  })
})
