import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'

// ─── Notification cards — sender header (2026-08-29) ────────────────────────
// Every card (inbox panel row + toast) opens with the chat header's agent
// identity: colored-initial avatar + display name, resolved from the cached
// agents list by the delivery's `agent_slug`. Unknown slugs fall back to a
// title-cased slug on the brand color (old rows / agents the viewer can't
// open); slug-less platform notifications get a neutral "OtoDock" header.

import NotificationPanel from '@/components/chat/notifications/NotificationPanel'
import NotificationToast from '@/components/chat/notifications/NotificationToast'
import NotificationAgentHeader, { titleCaseSlug } from '@/components/chat/notifications/NotificationAgentHeader'
import type { NotificationDelivery } from '@/api/notifications'
import type { AgentSummary } from '@/api/agents'

const agents = [
  { name: 'alpha', display_name: 'Alpha Prime', color: '#336699' },
  { name: 'no-color', display_name: 'Colorless', color: '' },
] as unknown as AgentSummary[]

const mkDelivery = (over: Partial<NotificationDelivery>): NotificationDelivery => ({
  id: 'd1',
  notification_id: null,
  title: 'Task done',
  body: 'All good',
  severity: 'info',
  scope: 'user',
  source: 'mcp',
  delivered_at: new Date().toISOString(),
  read: 0,
  dismissed: 0,
  read_at: null,
  dismissed_at: null,
  agent_slug: 'alpha',
  chat_id: null,
  ...over,
})

const noop = () => {}

describe('NotificationAgentHeader', () => {
  it('title-cases a slug for the fallback name', () => {
    expect(titleCaseSlug('personal-assistant-lite')).toBe('Personal Assistant Lite')
  })

  it('renders the resolved agent: initial on its color + display name', () => {
    render(<NotificationAgentHeader agentSlug="alpha" agents={agents} />)
    const header = screen.getByTestId('notification-agent-header')
    expect(header.getAttribute('data-agent')).toBe('alpha')
    expect(within(header).getByText('Alpha Prime')).toBeTruthy()
    const avatar = within(header).getByText('A')
    expect(avatar.style.backgroundColor).toBe('rgb(51, 102, 153)')
    expect(avatar.className).not.toContain('bg-brand')
  })

  it('falls back to the brand color when the agent has no color', () => {
    render(<NotificationAgentHeader agentSlug="no-color" agents={agents} />)
    const avatar = within(screen.getByTestId('notification-agent-header')).getByText('N')
    expect(avatar.className).toContain('bg-brand')
    expect(screen.getByText('Colorless')).toBeTruthy()
  })

  it('falls back to a title-cased slug when the slug is not in the list (or the list is absent)', () => {
    const { unmount } = render(<NotificationAgentHeader agentSlug="ghost-agent" agents={agents} />)
    expect(screen.getByText('Ghost Agent')).toBeTruthy()
    unmount()
    render(<NotificationAgentHeader agentSlug="ghost-agent" />)
    expect(screen.getByText('Ghost Agent')).toBeTruthy()
  })

  it('renders the neutral OtoDock header without a slug', () => {
    render(<NotificationAgentHeader agentSlug={null} agents={agents} />)
    const header = screen.getByTestId('notification-agent-header')
    expect(header.getAttribute('data-agent')).toBe('')
    expect(within(header).getByText('OtoDock')).toBeTruthy()
  })
})

describe('NotificationPanel — sender header per row', () => {
  it('shows the agent header on agent rows and the neutral header on platform rows', () => {
    render(
      <NotificationPanel
        deliveries={[
          mkDelivery({ id: 'a', agent_slug: 'alpha', title: 'Alpha said hi' }),
          mkDelivery({ id: 'b', agent_slug: null, title: 'Backup finished', source: 'system' }),
          mkDelivery({ id: 'c', agent_slug: 'legacy-bot', title: 'Old row' }),
        ]}
        loading={false}
        agents={agents}
        onMarkRead={noop}
        onMarkAllRead={noop}
        onDismiss={noop}
        onAcknowledge={noop}
        onClose={noop}
      />,
    )
    const headers = screen.getAllByTestId('notification-agent-header')
    expect(headers).toHaveLength(3)
    expect(headers.map((h) => h.getAttribute('data-agent'))).toEqual(['alpha', '', 'legacy-bot'])
    expect(screen.getByText('Alpha Prime')).toBeTruthy()
    expect(screen.getByText('OtoDock')).toBeTruthy()
    expect(screen.getByText('Legacy Bot')).toBeTruthy()
    // Titles are untouched — the header carries the identity.
    expect(screen.getByText('Alpha said hi')).toBeTruthy()
    expect(screen.getByText('Backup finished')).toBeTruthy()
  })

  it('still renders every row when no agents list is available yet', () => {
    render(
      <NotificationPanel
        deliveries={[mkDelivery({ id: 'a', agent_slug: 'alpha' })]}
        loading={false}
        onMarkRead={noop}
        onMarkAllRead={noop}
        onDismiss={noop}
        onAcknowledge={noop}
        onClose={noop}
      />,
    )
    expect(screen.getByText('Alpha')).toBeTruthy()
    expect(screen.getByText('Task done')).toBeTruthy()
  })
})

describe('NotificationToast — sender header per card', () => {
  it('renders the header above the title', () => {
    render(
      <NotificationToast
        toasts={[
          { id: 't1', delivery: mkDelivery({ id: 't1', agent_slug: 'alpha' }), createdAt: Date.now() },
          { id: 't2', delivery: mkDelivery({ id: 't2', agent_slug: null, title: 'Disk almost full' }), createdAt: Date.now() },
        ]}
        agents={agents}
        onDismiss={vi.fn()}
      />,
    )
    const headers = screen.getAllByTestId('notification-agent-header')
    expect(headers.map((h) => h.getAttribute('data-agent'))).toEqual(['alpha', ''])
    expect(screen.getByText('Alpha Prime')).toBeTruthy()
    expect(screen.getByText('Disk almost full')).toBeTruthy()
  })
})
