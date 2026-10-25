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
