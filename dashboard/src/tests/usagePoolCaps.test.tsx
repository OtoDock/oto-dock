import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

// ─── Subscription pool caps on the Usage pages, the composer's budget
//     naming, and the chat card for a cap-refused warmup ───────────────────

const { saved, mutation, capRef, limitsRef, myLimitSaves } = vi.hoisted(() => ({
  saved: [] as unknown[],
  myLimitSaves: [] as unknown[],
  mutation: () => ({ mutate: (u: unknown) => { saved.push(u) }, isPending: false, error: null }),
  capRef: { current: null as unknown },
  limitsRef: { current: { limits: [] as unknown[] } },
}))

vi.mock('@/api/usage', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/usage')>()
  return {
    ...mod,
    useMyPoolCap: () => ({ data: capRef.current, isLoading: false }),
    useAdminPoolCap: () => ({ data: capRef.current, isLoading: false }),
    useSetMyPoolCap: mutation,
    useSetAdminPoolCap: mutation,
    useMyLimits: () => ({ data: limitsRef.current }),
    useSetMyLimit: () => ({ mutate: (u: unknown) => { myLimitSaves.push(u) }, isPending: false }),
  }
})

import { MyPoolCapSection, PlatformPoolCapSection } from '@/components/usage/PoolCapSection'
import { MyApiKeysSection } from '@/pages/UserSettings.usage'
import { describeLimitReached, describeLimitWarning, hitText } from '@/components/usage/poolCap'
import { warmupFailSubtype } from '@/hooks/useChatStream'
import SystemEvent from '@/components/chat/SystemEvent'
import type { PoolCapStatus } from '@/api/usage'

function status(overrides: Partial<PoolCapStatus> = {}): PoolCapStatus {
  return {
    scope: 'user', layer: 'claude-code-cli', configured: true, accounts: 2,
    caps: { week_pct: 50, day_pct: null, week_usd: 40, day_usd: null },
    readings: { week_pct: 52, day_pct: 9, week_usd: 12.5, day_usd: 6.1 },
    on_reached: 'stop', hits: ['week_pct'], warning: true, allowed: false,
    ...overrides,
  }
}

describe('pool cap section', () => {
  it('renders one line per engine, coloured against the caps, and saves the form', () => {
    capRef.current = {
      caps: { week_pct: 50, day_pct: null, week_usd: 40, day_usd: null },
      on_reached: 'stop',
      engines: {
        'claude-code-cli': status(),
        'codex-cli': status({ layer: 'codex-cli', accounts: 1, hits: [], warning: false, allowed: true,
                              readings: { week_pct: 10, day_pct: 2, week_usd: 1, day_usd: 0 } }),
      },
    }
    render(<MyPoolCapSection />)
    expect(screen.getByText('My subscriptions')).toBeInTheDocument()
    expect(screen.getByText(/cap your own connected accounts/)).toBeInTheDocument()
    // The note behind the ⓘ opens in place.
    expect(screen.queryByTestId('pool-cap-info')).toBeNull()
    fireEvent.click(screen.getByLabelText('More about this cap'))
    expect(screen.getByTestId('pool-cap-info')).toHaveTextContent('all the enabled subscriptions')
    const lines = screen.getAllByTestId('pool-engine')
    expect(lines).toHaveLength(2)
    expect(lines[0]).toHaveTextContent('Claude Code')
    expect(lines[0]).toHaveTextContent('2 accounts')
    // One labelled cell per reading; the rolling periods are named as such.
    const week = lines[0].querySelector('[data-testid="pool-reading-week_pct"]')!
    expect(week).toHaveTextContent('Weekly window')
    expect(week).toHaveTextContent('52%')
    expect(week).toHaveTextContent('/ 50%')
    expect(week.querySelector('.text-p-error')).toHaveTextContent('52%')
    expect(lines[0].querySelector('[data-testid="pool-reading-week_usd"]')).toHaveTextContent('API cost, 7 days')
    expect(lines[0].querySelector('[data-testid="pool-reading-week_usd"]')).toHaveTextContent('$12.50')
    expect(lines[0].querySelector('[data-testid="pool-reading-day_usd"]')).toHaveTextContent('API cost, 24 h')
    expect(lines[1]).toHaveTextContent('Codex')
    expect(lines[1].querySelector('.text-p-error')).toBeNull()
    // The form starts from the row and posts every field (empty = null).
    expect(screen.getByLabelText('Week %')).toHaveValue(50)
    fireEvent.change(screen.getByLabelText('Week %'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('Day $'), { target: { value: '2.5' } })
    fireEvent.change(screen.getByLabelText('When reached'), { target: { value: 'continue' } })
    fireEvent.click(screen.getByText('Save'))
    expect(saved).toEqual([{ on_reached: 'continue', week_pct: null, day_pct: null, week_usd: 40, day_usd: 2.5 }])
  })

  it('says when the pool has no accounts and still offers the form', () => {
    capRef.current = { caps: { week_pct: null, day_pct: null, week_usd: null, day_usd: null }, on_reached: 'stop', engines: {} }
    render(<PlatformPoolCapSection />)
    expect(screen.getByText('Subscription pool')).toBeInTheDocument()
    expect(screen.getByText(/cap the accounts in the agent pool/)).toBeInTheDocument()
    expect(screen.getByText('No subscription accounts in the agent pool.')).toBeInTheDocument()
    expect(screen.getByLabelText('Week %')).toHaveValue(null)
    expect(screen.getByText(/Less than 14.3% a day/)).toBeInTheDocument()
  })
})

describe('my API keys', () => {
  it('shows the bars and posts the cap per period', () => {
    limitsRef.current = { limits: [{ id: 1, limit_type: 'user_self', target: 'u', period: 'monthly', cost_limit_usd: 20, updated_at: '', updated_by: 'u' }] }
    render(<MyApiKeysSection selfLimits={{
      monthly: { limit: 20, used: 17, percent: 85, start: '', end: '' },
      weekly: { limit: null, used: 3, percent: 0, start: '', end: '' },
    }} />)
    expect(screen.getByText('My API keys')).toBeInTheDocument()
    expect(screen.getByText('$17.00 / $20.00')).toBeInTheDocument()
    expect(screen.getByText('85%')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Set cap'))
    expect(screen.getByLabelText('Monthly ($)')).toHaveValue(20)
    fireEvent.change(screen.getByLabelText('Weekly ($)'), { target: { value: '5' } })
    fireEvent.click(screen.getByText('Save'))
    expect(myLimitSaves).toEqual([
      { period: 'monthly', cost_limit_usd: 20 },
      { period: 'weekly', cost_limit_usd: 5 },
    ])
  })
})

describe('the composer names the budget', () => {
  it('pool cap, own key budget, platform budget', () => {
    expect(hitText(status())).toBe('the week is at 52% of the 50% cap')
    const pool = describeLimitReached({ pool: status() })
    expect(pool.title).toBe('Subscription cap reached.')
    expect(pool.body).toContain('Claude Code: the week is at 52% of the 50% cap')
    expect(pool.body).toContain('User Settings → Usage')
    const own = describeLimitReached({ self: { monthly: { limit: 10, used: 10.5, percent: 105, start: '', end: '' } } })
    expect(own.title).toBe('Your API-key budget is reached.')
    expect(own.body).toContain('$10.50 of $10.00')
    expect(describeLimitReached({ monthly: { limit: 50, used: 51, percent: 102, start: '', end: '' } }).title)
      .toBe('Usage limit reached.')
    expect(describeLimitReached(null).body).toContain('administrator')
  })

  it('warning text: continuing on a key, a near cap, an own-key budget, the platform budget', () => {
    expect(describeLimitWarning({ pool: status({ on_reached: 'continue' }) }))
      .toBe('Subscription cap reached (the week is at 52% of the 50% cap); continuing on an API key.')
    expect(describeLimitWarning({ pool: status({ hits: [], allowed: true, readings: { week_pct: 42, day_pct: 1, week_usd: 1, day_usd: 0 } }) }))
      .toBe('Subscription cap: the week is at 42% of the 50% cap on Claude Code.')
    expect(describeLimitWarning({ self: { weekly: { limit: 5, used: 4.2, percent: 84, start: '', end: '' } } }))
      .toBe("You've used 84% of your weekly API-key budget ($4.20 / $5.00).")
    expect(describeLimitWarning({ weekly: { limit: 50, used: 45, percent: 90, start: '', end: '' } }))
      .toContain('90% of your weekly limit')
    expect(describeLimitWarning(null)).toBe('You are approaching your usage limit.')
  })
})

describe('a cap-refused warmup', () => {
  it('maps to its own card with the proxy wording and a way to the Usage tab', () => {
    expect(warmupFailSubtype('pool_cap')).toBe('pool_cap')
    render(
      <MemoryRouter>
        <SystemEvent subtype="pool_cap" message="Your Claude subscription cap is reached: the week is at 52% of the 50% cap." />
      </MemoryRouter>,
    )
    expect(screen.getByText('Subscription cap reached')).toBeInTheDocument()
    expect(screen.getByText(/the week is at 52%/)).toBeInTheDocument()
    expect(screen.getByText('Open the Usage tab')).toHaveAttribute('href', '/user-settings?tab=usage')
  })
})
