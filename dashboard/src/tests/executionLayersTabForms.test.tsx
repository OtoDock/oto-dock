import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'

// ─── Admin, AI Engines, an engine's subscriptions: "+ API Key" opens its form
//     under the section's buttons, above the rows; a row's Discover opens its
//     panel under that row; "+ Connect Account" opens a dialog ───

vi.mock('@/pages/admin/ExecutionLayersTab.sections', () => ({
  SetupBanner: () => null,
  ConnectOtodockPrompt: () => null,
  HostedRelaySection: () => null,
}))
vi.mock('@/lib/nativeBridge', () => ({ setNativeAuthInProgress: vi.fn() }))
vi.mock('@/api/executionLayers', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/executionLayers')>()
  const { claudeLike } = await import('./fixtures/engines')
  const idle = () => ({ mutate: () => {}, mutateAsync: async () => ({}), isPending: false, isError: false, error: null })
  const key = (id: string, label: string) => ({
    id, label, provider: 'anthropic', auth_type: 'api_key', status: 'active', oauth_email: '',
    active_sessions: 0, use_personal: true, contribute_platform: true, is_mine: true, layer: 'claude-code-cli',
  })
  return {
    ...mod,
    useAdminExecutionLayers: () => ({
      data: [{
        name: 'claude-code-cli', display_name: 'Anthropic Claude Code', capabilities: claudeLike(),
        subscriptions: { platform: [key('k1', 'Key one'), key('k2', 'Key two'), key('k3', 'Key three')], user_count: 0 },
        models: [], pool_stats: {},
      }],
      isLoading: false, error: null,
    }),
    useDiscoverModels: () => ({
      mutate: (_v: unknown, opts?: { onSuccess?: (d: unknown) => void }) => opts?.onSuccess?.({
        provider: 'anthropic', models: [{ model_id: 'claude-new', display_name: 'Claude New' }],
      }),
      isPending: false, isError: false, error: null,
    }),
    useAddSubscription: idle,
    useUpdateSubscription: idle,
    useDeleteSubscription: idle,
    useBulkAddModels: idle,
    useAddModel: idle,
    useUpdateModel: idle,
    useDeleteModel: idle,
    useStartClaudeOAuth: idle,
    useExchangeClaudeOAuth: idle,
    useStartOpenAIOAuth: idle,
    useOpenAIOAuthStatus: idle,
    useFinishOpenAIOAuth: idle,
  }
})

import ExecutionLayersTab from '@/pages/admin/ExecutionLayersTab'
import { SubscriptionRow } from '@/pages/admin/ExecutionLayersTab.rows'
import type { Subscription } from '@/api/executionLayers'

const follows = (a: Node, b: Node) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)

function openCard() {
  render(<ExecutionLayersTab />)
  fireEvent.click(screen.getByText('Claude Code CLI'))
}

describe('Admin AI Engines: forms open where they are invoked', () => {
  it('"+ API Key" opens its form above the subscription rows', () => {
    openCard()
    fireEvent.click(screen.getByRole('button', { name: '+ API Key' }))
    const field = screen.getByLabelText('API key')
    expect(follows(field, screen.getByText('Key one'))).toBe(true)
  })

  it("a row's Discover opens its panel under that row, before the next", () => {
    openCard()
    // One Discover per key row, in row order: the second is Key two's.
    fireEvent.click(screen.getAllByRole('button', { name: 'Discover' })[1])
    const panel = screen.getByText('Discovered Models')
    expect(follows(screen.getByText('Key two'), panel)).toBe(true)
    expect(follows(panel, screen.getByText('Key three'))).toBe(true)
  })

  it('"+ Connect Account" opens a dialog', () => {
    openCard()
    fireEvent.click(screen.getByRole('button', { name: '+ Connect Account' }))
    const dialog = screen.getByRole('dialog', { name: 'Connect a Claude account' })
    expect(within(dialog).getByRole('button', { name: 'Connect Claude Account' })).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('Admin AI Engines: a login that ends soon', () => {
  const inHours = (hours: number) => new Date(Date.now() + hours * 3_600_000).toISOString()
  const oauthRow = (over: object) => ({
    id: 'o1', label: 'Claude Max', provider: 'anthropic', auth_type: 'oauth', status: 'active',
    oauth_email: 'a@example.com', active_sessions: 0, use_personal: true, contribute_platform: true,
    is_mine: true, owner_sub: 'local:admin', layer: 'claude-code-cli', created_at: '', updated_at: '',
    login_expires_at: inHours(50), ...over,
  }) as unknown as Subscription
  const renderRow = (over: object = {}) =>
    render(<SubscriptionRow sub={oauthRow(over)} layer="claude-code-cli" vendorId="anthropic" />)
  const hint = () => {
    fireEvent.click(screen.getByRole('button', { name: 'About Login ends in 2 days' }))
    return screen.getByRole('tooltip')
  }

  it("the admin's own row: the chip, and Connect Account renews it", () => {
    renderRow()
    expect(screen.getByText('Login ends in 2 days')).toBeInTheDocument()
    expect(hint()).toHaveTextContent('To renew it, press Connect Account above and sign in as the same account.')
  })

  it("another admin's row: its owner renews it", () => {
    renderRow({ is_mine: false, owner_sub: 'local:other' })
    expect(hint()).toHaveTextContent('Its owner renews it by signing in to the same account again.')
  })

  it('an owner-less row: it cannot be renewed in place', () => {
    renderRow({ is_mine: false, owner_sub: '' })
    expect(hint()).toHaveTextContent('Nobody owns this account, so it cannot be renewed in place.')
  })

  it('at four days, and on an expired row, nothing new shows', () => {
    const { unmount } = renderRow({ login_expires_at: inHours(96) })
    expect(screen.queryByText(/Login end/)).toBeNull()
    unmount()
    renderRow({ status: 'expired', login_expires_at: inHours(10) })
    expect(screen.queryByText(/Login end/)).toBeNull()
    expect(screen.getByText('expired')).toBeInTheDocument()
  })
})
