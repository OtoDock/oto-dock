// A person with no AI engine uses the platform: no gate, the admin keeps the
// strip, and the warm-up's own refusal is the notice where an engine is needed.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ComponentProps } from 'react'

const h = vi.hoisted(() => ({ user: null as Record<string, unknown> | null }))

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: h.user, logout: vi.fn() }),
}))
vi.mock('@/api/agents', () => ({
  useExecutionLayers: () => ({ data: undefined }),
}))

import SetupBanner from '@/components/SetupBanner'
import EngineNotice from '@/components/common/EngineNotice'
import SystemEvent from '@/components/chat/SystemEvent'
import ChatBanners from '@/pages/agent/chat/ChatBanners'
import { newChatEngineNotice, type PreWarmRefusal } from '@/lib/engines/notice'
import type { User } from '@/api/auth'

const ADMIN = { sub: 'a', role: 'admin', agents: [], agent_roles: {}, has_own_engine: false } as unknown as User
const MEMBER = { sub: 'm', role: 'member', agents: [], agent_roles: {}, has_own_engine: false } as unknown as User

describe('the strip is an admin\'s', () => {
  beforeEach(() => { h.user = null })

  it('an admin with no engine of their own keeps the connect strip', () => {
    h.user = ADMIN as unknown as Record<string, unknown>
    render(<MemoryRouter><SetupBanner /></MemoryRouter>)
    expect(screen.getByText('Connect an AI engine')).toBeTruthy()
  })

  it('a member with no engine gets no strip on any page', () => {
    h.user = MEMBER as unknown as Record<string, unknown>
    const { container } = render(<MemoryRouter><SetupBanner /></MemoryRouter>)
    expect(container.textContent).toBe('')
  })
})

describe('the notice on a new chat comes from the warm-up\'s refusal', () => {
  const refusal: PreWarmRefusal = {
    agent: 'ops', executionPath: 'claude-code-cli', reason: 'auth_off',
    error: 'This runs on an AI engine you have not connected yet.',
  }

  it('shows the refusal on a new chat of its agent and engine', () => {
    expect(newChatEngineNotice(refusal, 'ops', 'claude-code-cli', null, MEMBER)).toEqual({
      message: 'This runs on an AI engine you have not connected yet.', reason: 'auth_off',
    })
  })

  it('shows nothing with no refusal, so a person who can run the agent never sees it', () => {
    expect(newChatEngineNotice(null, 'ops', 'claude-code-cli', null, MEMBER)).toBeNull()
  })

  it('shows nothing on an existing chat, another agent or another engine', () => {
    expect(newChatEngineNotice(refusal, 'ops', 'claude-code-cli', 'chat-1', MEMBER)).toBeNull()
    expect(newChatEngineNotice(refusal, 'other', 'claude-code-cli', null, MEMBER)).toBeNull()
    expect(newChatEngineNotice(refusal, 'ops', 'codex-cli', null, MEMBER)).toBeNull()
  })

  it('leaves a rest to the send, and stays quiet under an admin\'s strip', () => {
    expect(newChatEngineNotice({ ...refusal, reason: 'throttled' }, 'ops', 'claude-code-cli', null, MEMBER)).toBeNull()
    expect(newChatEngineNotice(refusal, 'ops', 'claude-code-cli', null, ADMIN)).toBeNull()
  })

  it('draws over the composer through ChatBanners', () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <ChatBanners {...BANNERS} engineNotice={{ message: refusal.error, reason: refusal.reason }} />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const notice = screen.getByTestId('engine-notice')
    expect(notice.textContent).toContain('This runs on an AI engine you have not connected yet.')
    expect(screen.getByRole('link', { name: 'Connect an AI engine' }).getAttribute('href'))
      .toBe('/user-settings?tab=ai-engines')
  })
})

describe('EngineNotice', () => {
  it.each(['auth_off', 'admin_oauth_only', 'no_pool', 'none', 'own_sub_expired'])(
    'offers the connect button for %s', (reason) => {
      render(<MemoryRouter><EngineNotice message="Sentence." reason={reason} /></MemoryRouter>)
      expect(screen.getByRole('link', { name: 'Connect an AI engine' })).toBeTruthy()
    },
  )

  it('keeps the sentence alone when connecting would not help', () => {
    render(<MemoryRouter><EngineNotice message="Ask an admin." reason="local_only" /></MemoryRouter>)
    expect(screen.getByText('Ask an admin.')).toBeTruthy()
    expect(screen.queryByRole('link')).toBeNull()
  })
})

describe('the refusal cards', () => {
  it('a send the warm-up refused shows the sentence and the button', () => {
    render(<MemoryRouter><SystemEvent subtype="no_subscription" message="Connect one." reason="auth_off" /></MemoryRouter>)
    expect(screen.getByText('Connect one.')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Connect an AI engine' })).toBeTruthy()
    expect(screen.queryByText('Subscription required')).toBeNull()
  })

  it('a meeting the pool refused offers the connection, one that failed elsewhere does not', () => {
    const { unmount } = render(
      <MemoryRouter><SystemEvent subtype="meeting_failed" message="No engine." reason="auth_off" /></MemoryRouter>,
    )
    expect(screen.getByRole('link', { name: 'Connect an AI engine' })).toBeTruthy()
    unmount()
    render(<MemoryRouter><SystemEvent subtype="meeting_failed" message="Out of room." /></MemoryRouter>)
    expect(screen.queryByRole('link')).toBeNull()
  })
})

const BANNERS: ComponentProps<typeof ChatBanners> = {
  sessionExecutionTarget: 'local', sessionFallbackReason: null, offlineMachineName: '',
  engineNotice: null, pendingEngineSwitch: null, targetMismatch: null, phoneCallActive: false,
  platformUpdating: false, lockSentence: '', viewedStreaming: false, warming: false, ws: {} as ComponentProps<typeof ChatBanners>['ws'],
  agentName: 'ops', mode: 'ask', model: '', chatActiveLayer: null, selectedLayer: null,
  interactive: {} as ComponentProps<typeof ChatBanners>['interactive'], preWarmedRef: { current: null }, preWarmPath: '',
  layers: undefined, engineSwitchBusy: false, engineSwitchError: null, handleEngineSwitchConfirm: () => {},
  handleEngineSwitchCancel: () => {}, chatId: null,
}
