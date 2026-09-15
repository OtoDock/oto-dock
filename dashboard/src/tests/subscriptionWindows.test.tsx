import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import { SubscriptionWindowBars, BalanceHint, formatReset } from '@/components/engines/SubscriptionWindows'

const windows = {
  five_hour: { pct: 34.4, resets_at: '2099-01-01T14:00:00+00:00' },
  seven_day: { pct: 100, resets_at: '2099-01-03T09:00:00+00:00' },
  scoped: [{ key: 'fable', label: 'Fable', pct: 100, resets_at: '2099-01-03T09:00:00+00:00', active: true }],
  reached: 'seven_day', plan: 'max', observed_at: new Date(Date.now() - 3 * 60000).toISOString(), source: 'poll',
}

describe('SubscriptionWindowBars', () => {
  it('renders one bar per window with its reset instant and age', () => {
    render(<SubscriptionWindowBars windows={windows} />)
    expect(screen.getAllByTestId('window-bar')).toHaveLength(3)
    expect(screen.getByText('Session')).toBeInTheDocument()
    expect(screen.getByText('34%')).toBeInTheDocument()
    expect(screen.getByText('Week')).toBeInTheDocument()
    // A full window reads "until", a live one "resets".
    expect(screen.getAllByText(/^until /)).toHaveLength(2)
    expect(screen.getAllByText(/^resets /)).toHaveLength(1)
    // The full instant rides the hover title; on a phone the text drops to
    // its own line instead of truncating.
    const reset = screen.getByText(/^resets /)
    expect(reset.getAttribute('title')).toMatch(/\d{2}:\d{2}/)
    expect(reset.className).toContain('basis-full')
    expect(screen.getByText('as of 3 min ago')).toBeInTheDocument()
  })

  it('says so before the first sample and renders nothing when absent', () => {
    render(<SubscriptionWindowBars windows={null} />)
    expect(screen.getByText('Usage not read yet.')).toBeInTheDocument()
    const { container } = render(<SubscriptionWindowBars windows={undefined} />)
    expect(container.querySelector('[data-testid="subscription-windows"]')).toBeNull()
  })

  it('hint only with two or more accounts', () => {
    const { rerender } = render(<BalanceHint oauthCount={1} />)
    expect(screen.queryByTestId('balance-hint')).toBeNull()
    rerender(<BalanceHint oauthCount={2} />)
    expect(screen.getByTestId('balance-hint')).toHaveTextContent('Enable all your subscriptions')
  })

  it('formatReset tolerates junk', () => {
    expect(formatReset(null)).toBe('')
    expect(formatReset('not a date')).toBe('')
    expect(formatReset('2099-01-03T09:00:00+00:00')).toMatch(/\d/)
  })
})
