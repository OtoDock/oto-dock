/**
 * A subscription whose MCP was removed, or switched off, says so beside its
 * status, in its row and as a choice in a trigger's list: it receives
 * nothing until then.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SubscriptionRow } from './SubscriptionRow'
import { mcpStateSentence, subscriptionOptionLabel, type WebhookSubscription } from '../../api/subscriptions'

function sub(over: Partial<WebhookSubscription> = {}): WebhookSubscription {
  return {
    id: 's1', scope: 'user', owner: 'u1', agent: null, mcp_name: 'slack-mcp',
    provider_id: 'slack', account_label: 'a', vendor_target: 'T1',
    vendor_subscription_id: null, selected_events: ['reaction_added'],
    selected_subevents: {}, status: 'active', last_error: null, last_event_at: null,
    event_count: 0, expires_at: null, created_by: 'u1', created_at: '', updated_at: '',
    delivery_mode: 'relay', ...over,
  }
}

function row(s: WebhookSubscription) {
  return render(
    <ul>
      <SubscriptionRow sub={s} webhookBase="" registrationMode="relay" perSubscriptionSecret={false}
        onDelete={() => {}} isPending={false} />
    </ul>,
  )
}

describe('a subscription whose MCP is gone', () => {
  it('marks a removed MCP', () => {
    row(sub({ mcp_state: 'removed' }))
    expect(screen.getByTestId('subscription-mcp-state').textContent).toBe('Its MCP was removed')
  })

  it('marks an MCP turned off', () => {
    row(sub({ mcp_state: 'off' }))
    expect(screen.getByTestId('subscription-mcp-state').textContent).toBe('Its MCP is turned off')
  })

  it('marks nothing while the MCP is on, or for an older proxy', () => {
    row(sub({ mcp_state: 'on' }))
    row(sub())
    expect(screen.queryByTestId('subscription-mcp-state')).toBeNull()
  })

  it('says so in a trigger\'s choice of subscription', () => {
    expect(subscriptionOptionLabel(sub())).toBe('slack/T1 · reaction_added')
    expect(subscriptionOptionLabel(sub({ mcp_state: 'removed' })))
      .toBe('slack/T1 · reaction_added (Its MCP was removed)')
  })

  it('says it in full under the list, where a phone cuts the choice short', () => {
    expect(mcpStateSentence('off')).toBe(
      'Its MCP is turned off. It receives no events until an admin turns the MCP on again.')
    expect(mcpStateSentence('on')).toBeUndefined()
    expect(mcpStateSentence(undefined)).toBeUndefined()
  })
})
