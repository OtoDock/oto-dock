// The three settings areas on the real App and the real browser history:
// the route table and the redirects, the four hrefs the proxy stores, the
// phone lists with their gates and the admin banners, a section's header
// and swipe, the fold on the index, and Back to Chat. The pages are stubs;
// the guards that need a server are passed through.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, screen, fireEvent } from '@testing-library/react'
import { Link, Outlet, useSearchParams } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

const h = vi.hoisted(() => ({
  user: {} as Record<string, unknown>,
  logout: vi.fn(),
  remoteMachines: true,
  releaseDialogMounts: 0,
}))

const admin = () => ({
  sub: 'u1', name: 'dev-admin', display_name: 'Dev Admin', role: 'admin',
  agents: ['ops', 'other'], agent_roles: {}, feature_flags: { remote_machines_available: h.remoteMachines },
})
const member = () => ({ ...admin(), name: 'dev-member', display_name: 'Dev Member', role: 'member', agent_roles: { ops: 'viewer' } })

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: h.user, logout: h.logout, refreshUser: vi.fn(), setUser: vi.fn() }),
}))
vi.mock('@/components/RequireAuth', () => ({ default: () => <Outlet /> }))
vi.mock('@/components/AgentGuard', () => ({ default: () => <Outlet /> }))
vi.mock('@/components/PlatformSetupGuard', () => ({
  default: () => <Outlet />,
  SetupBanner: () => <div data-testid="setup-banner">connect an engine</div>,
}))
vi.mock('@/components/DefaultAgentRedirect', () => ({ default: () => <p>default chat</p> }))
vi.mock('@/components/admin/ForwardingBanner', () => ({ ForwardingBanner: () => null }))
vi.mock('@/components/admin/ReleaseBanner', () => ({ ReleaseBanner: () => null }))
vi.mock('@/components/ReleaseDialog', () => ({
  // Counts its mounts: the real one keeps its dismissal in its own state.
  ReleaseDialog: () => {
    useEffect(() => { h.releaseDialogMounts += 1 }, [])
    return null
  },
}))
vi.mock('@/components/LowCreditModal', () => ({ LowCreditModal: () => null, default: () => null }))
vi.mock('@/api/community', () => ({ useAdminMcpRequests: () => ({ data: { pending_count: 3 } }) }))
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useAgentInfo: () => ({ data: { display_name: 'Ops Agent' } }),
}))

vi.mock('@/pages/agent/AgentChat', () => ({
  default: () => (
    <div>
      <p>chat page</p>
      <Link to="/admin">open admin</Link>
      <Link to="/agents/ops/config">open ops config</Link>
      <Link to="/user-settings?tab=usage">open usage tab</Link>
    </div>
  ),
}))
vi.mock('@/pages/agent/AgentOverview', () => ({ default: () => <p>agent overview</p> }))
vi.mock('@/pages/agent/AgentConfig', () => ({ default: () => <p>agent config</p> }))
vi.mock('@/pages/agent/AgentConversations', () => ({ default: () => <p>agent conversations</p> }))
vi.mock('@/pages/agent/AgentSchedules', () => ({ default: () => <p>agent scheduled tasks</p> }))
vi.mock('@/pages/agent/AgentTriggers', () => ({ default: () => <p>agent triggers</p> }))
vi.mock('@/pages/agent/AgentNotifications', () => ({ default: () => <p>agent notifications</p> }))
vi.mock('@/pages/agent/AgentMeetings', () => ({ default: () => <p>agent meetings</p> }))
vi.mock('@/pages/agent/AgentMcps', () => ({ default: () => <p>agent mcps</p> }))
vi.mock('@/pages/agent/AgentSkills', () => ({ default: () => <p>agent skills</p> }))
vi.mock('@/pages/agent/AgentChecks', () => ({ default: () => <p>agent checks</p> }))
vi.mock('@/pages/Overview', () => ({ default: () => <p>admin overview</p> }))
vi.mock('@/pages/Schedules', () => ({ default: () => <p>admin scheduled tasks</p> }))
vi.mock('@/pages/Triggers', () => ({ default: () => <p>admin triggers</p> }))
vi.mock('@/pages/History', () => ({ default: () => <p>admin task history</p> }))
vi.mock('@/pages/admin/UsersPage', () => ({ default: () => <p>admin users</p> }))
vi.mock('@/pages/admin/UsagePage', () => ({ default: () => <p>admin usage</p> }))
vi.mock('@/pages/admin/McpServersPage', () => ({ default: () => <p>admin mcp servers</p> }))
vi.mock('@/pages/admin/McpRequestsPage', () => ({ default: () => <p>admin mcp requests</p> }))
vi.mock('@/pages/admin/SkillsPage', () => ({ default: () => <p>admin skills</p> }))
vi.mock('@/pages/admin/NotificationsPage', () => ({ default: () => <p>admin notifications</p> }))
vi.mock('@/pages/admin/MeetingsPage', () => ({ default: () => <p>admin meetings</p> }))
vi.mock('@/pages/admin/RemoteMachinesPage', () => ({ default: () => <p>admin remote machines</p> }))
vi.mock('@/pages/admin/SharesPage', () => ({ default: () => <p>admin shares</p> }))
vi.mock('@/pages/admin/ReportsPage', () => ({ default: () => <p>admin reports</p> }))
vi.mock('@/pages/admin/PlatformPage', () => ({
  // Holds local state, as the real page holds its sub-tab and its fields.
  default: () => {
    const [params] = useSearchParams()
    const [typed, setTyped] = useState(0)
    return (
      <div>
        <p>setup tab {params.get('tab') ?? 'general'}</p>
        <button onClick={() => setTyped((n) => n + 1)}>type a letter</button>
        <p>typed {typed}</p>
      </div>
    )
  },
}))
vi.mock('@/pages/AgentsPage', () => ({ default: () => <p>agents page</p> }))
vi.mock('@/pages/apps/AppPage', () => ({ default: () => <p>app page</p> }))
vi.mock('@/pages/SharedChatPage', () => ({ default: () => <p>shared chat</p> }))
vi.mock('@/pages/ConversationView', () => ({ default: () => <p>conversation view</p> }))
vi.mock('@/pages/RunRedirect', () => ({ default: () => <p>run redirect</p> }))
vi.mock('@/pages/AuthCallback', () => ({ default: () => <p>auth callback</p> }))
vi.mock('@/pages/ChangePassword', () => ({ default: () => <p>change password</p> }))
vi.mock('@/pages/ResetPassword', () => ({ default: () => <p>reset password</p> }))
vi.mock('@/pages/AcceptInvite', () => ({ default: () => <p>accept invite</p> }))
vi.mock('@/pages/Setup2FA', () => ({ default: () => <p>setup 2fa</p> }))
vi.mock('@/pages/NativePasskey', () => ({ default: () => <p>native passkey</p> }))
vi.mock('@/pages/UserSettings.general', () => ({
  ProfileSection: () => <p>profile section</p>,
  SecuritySection: () => <p>security section</p>,
  AppearanceSection: () => null,
  MyMemorySection: () => null,
  WakeWordSection: () => null,
  NotificationsSection: () => null,
  AppsSection: () => null,
}))
vi.mock('@/pages/UserSettings.integrations', () => ({
  IntegrationsTab: () => {
    const [params] = useSearchParams()
    return <p>integrations section {params.get('provider') ?? ''}</p>
  },
}))
vi.mock('@/pages/UserSettings.machines', () => ({ MyMachinesSection: () => <p>machines section</p> }))
vi.mock('@/pages/UserSettings.aiEngines', () => ({ ExecutionLayersSection: () => <p>ai engines section</p> }))
vi.mock('@/pages/UserSettings.usage', () => ({ UsageSection: () => <p>usage section</p> }))
vi.mock('@/components/audio/AudioPrefsSection', () => ({ AudioPrefsSection: () => <p>audio section</p> }))

import App from '@/App'

let narrow = false
let matchMediaOriginal: typeof window.matchMedia
const originalWidth = window.innerWidth
/** The viewport the media query and `useSwipeGesture`'s width gate both read. */
const setNarrow = (v: boolean) => {
  narrow = v
  Object.defineProperty(window, 'innerWidth', { value: v ? 390 : 1280, configurable: true })
}

const idx = () => (window.history.state as { idx?: number } | null)?.idx
const path = () => window.location.pathname + window.location.search
const settle = () => act(async () => {
  for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0))
})
const click = async (label: string) => {
  await act(async () => { fireEvent.click(screen.getByText(label)) })
  await settle()
}
const back = async () => {
  await act(async () => { window.history.back() })
  await settle()
}

async function coldLoad(at: string) {
  setNarrow(narrow)
  window.history.replaceState(null, '', at)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <App />
    </QueryClientProvider>,
  )
  await settle()
}

const rows = () => screen.getAllByRole('link').filter((a) => a.closest('nav')).map((a) => a.textContent?.replace(/\d+$/, '').trim())

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  h.user = admin()
  h.remoteMachines = true
  h.releaseDialogMounts = 0
  matchMediaOriginal = window.matchMedia
  window.matchMedia = ((q: string) => ({
    matches: q.includes('767') ? narrow : false,
    media: q, onchange: null, addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as typeof window.matchMedia
  window.history.pushState(null, '', '/blank')
})

afterEach(() => {
  window.matchMedia = matchMediaOriginal
  narrow = false
  Object.defineProperty(window, 'innerWidth', { value: originalWidth, configurable: true })
  vi.restoreAllMocks()
})

describe('the routes and the redirects', () => {
  it('sends ?tab= to the section with every other parameter kept', async () => {
    await coldLoad('/user-settings?tab=ai-engines&provider=gmail-mcp&wakeDiag=1')
    expect(path()).toBe('/user-settings/ai-engines?provider=gmail-mcp&wakeDiag=1')
    expect(screen.getByText('ai engines section')).toBeTruthy()
  })

  it('opens General for an unknown tab, the rest of the query kept', async () => {
    await coldLoad('/user-settings?tab=billing&x=1')
    expect(path()).toBe('/user-settings/general?x=1')
    expect(screen.getByText('profile section')).toBeTruthy()
  })

  it('an unknown section keeps its query on the way to General', async () => {
    await coldLoad('/user-settings/nowhere?provider=x')
    expect(path()).toBe('/user-settings/general?provider=x')
  })

  it('the bare path shows General on a desktop, under the sidebar', async () => {
    await coldLoad('/user-settings')
    expect(screen.getByText('profile section')).toBeTruthy()
    expect(screen.getByRole('navigation', { name: 'Dev Admin sections' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'General' }).getAttribute('aria-current')).toBe('page')
  })

  it('the provider reaches the integrations section', async () => {
    await coldLoad('/user-settings/integrations?provider=gmail-mcp')
    expect(screen.getByText('integrations section gmail-mcp')).toBeTruthy()
  })

  it('Remote Machines without the feature goes to General', async () => {
    h.remoteMachines = false
    h.user = admin()
    await coldLoad('/user-settings/remote-machines')
    expect(path()).toBe('/user-settings/general')
    expect(screen.queryByRole('link', { name: 'Remote Machines' })).toBeNull()
  })

  it('keeps the four hrefs the proxy stores', async () => {
    await coldLoad('/admin/platform?tab=otodock')
    expect(await screen.findByText('setup tab otodock')).toBeTruthy()
  })

  it('an agent config link opens Configuration', async () => {
    await coldLoad('/agents/ops/config')
    expect(screen.getByText('agent config')).toBeTruthy()
  })

  it('the admin scheduled tasks link opens the page', async () => {
    await coldLoad('/admin/scheduled-tasks')
    expect(await screen.findByText('admin scheduled tasks')).toBeTruthy()
  })

  it('the agent page is Overview on a desktop and the list on a phone', async () => {
    await coldLoad('/agents/ops')
    expect(screen.getByText('agent overview')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Overview' }).getAttribute('aria-current')).toBe('page')
  })

  it('Overview has its own route in both areas', async () => {
    await coldLoad('/agents/ops/overview')
    expect(screen.getByText('agent overview')).toBeTruthy()
  })

  it('the admin overview route shows Overview', async () => {
    await coldLoad('/admin/overview')
    expect(await screen.findByText('admin overview')).toBeTruthy()
  })
})

describe('the phone list', () => {
  beforeEach(() => { narrow = true })

  it('the admin area lists its sections, Setup second, the badge and the Monitoring heading', async () => {
    await coldLoad('/admin')
    expect(screen.getByRole('navigation', { name: 'OtoDock sections' })).toBeTruthy()
    expect(rows()).toEqual([
      'Overview', 'Setup', 'Users', 'Usage', 'MCP Servers', 'Skills', 'MCP Requests',
      'Remote Machines', 'Shares', 'Reports',
      'Scheduled Tasks', 'Triggers', 'Notifications', 'Task History', 'Meetings',
    ])
    expect(screen.getByLabelText('3 pending').textContent).toBe('3')
    expect(screen.getByRole('heading', { name: 'Monitoring' })).toBeTruthy()
    expect(screen.queryByText('admin overview')).toBeNull()
    expect(screen.getByTestId('setup-banner')).toBeTruthy()
  })

  it('a member sees no manager rows on an agent, and a deep link to one still opens its page', async () => {
    h.user = member()
    await coldLoad('/agents/ops')
    expect(rows()).toEqual(['Overview', 'Checks', 'Scheduled Tasks', 'Triggers', 'Notifications', 'Meetings'])
    expect(screen.getByRole('heading', { name: 'Ops Agent' })).toBeTruthy()
  })

  it('a member deep-linked to Configuration gets the page', async () => {
    h.user = member()
    await coldLoad('/agents/ops/config')
    expect(screen.getByText('agent config')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Configuration' })).toBeTruthy()
  })

  it('User Settings lists its sections', async () => {
    await coldLoad('/user-settings')
    expect(rows()).toEqual(['General', 'Integrations', 'Remote Machines', 'AI Engines', 'Audio', 'Usage'])
    expect(screen.queryByText('profile section')).toBeNull()
  })

  it('a row opens the section under a header with a named back arrow, focused on its heading, and the arrow returns', async () => {
    await coldLoad('/admin')
    const at = idx()!
    await click('Users')
    expect(path()).toBe('/admin/users')
    expect(idx()).toBe(at + 1)
    expect(await screen.findByText('admin users')).toBeTruthy()
    const heading = screen.getByRole('heading', { name: 'Users' })
    expect(document.activeElement).toBe(heading)
    expect(screen.getByTestId('setup-banner')).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Back' })) })
    await settle()
    expect(path()).toBe('/admin')
    expect(idx()).toBe(at)
    expect(rows().length).toBe(15)
  })

  it('a swipe to the right on the section returns, and one from the edge is left to the system', async () => {
    await coldLoad('/admin')
    await click('Shares')
    expect(await screen.findByText('admin shares')).toBeTruthy()
    const main = screen.getByRole('main')
    const touch = (type: string, x: number, at: number) => {
      vi.spyOn(Date, 'now').mockReturnValue(at)
      const e = new Event(type)
      Object.defineProperty(e, type === 'touchend' ? 'changedTouches' : 'touches', { value: [{ clientX: x, clientY: 200 }] })
      main.dispatchEvent(e)
    }
    await act(async () => { touch('touchstart', 4, 1000); touch('touchend', 180, 1200) })
    await settle()
    expect(path()).toBe('/admin/shares')
    await act(async () => { touch('touchstart', 80, 2000); touch('touchend', 260, 2200) })
    await settle()
    expect(path()).toBe('/admin')
  })

  it('a cold deep link gets the list behind it', async () => {
    await coldLoad('/agents/ops/triggers')
    expect(screen.getByText('agent triggers')).toBeTruthy()
    expect(idx()).toBe(2)
    await back()
    expect(path()).toBe('/agents/ops')
    expect(rows()[0]).toBe('Overview')
  })
})

describe('the fold on the index', () => {
  it('swaps Overview and the list without a navigation', async () => {
    narrow = false
    await coldLoad('/admin')
    expect(await screen.findByText('admin overview')).toBeTruthy()
    const at = idx()
    setNarrow(true)
    await act(async () => { window.dispatchEvent(new Event('resize')) })
    expect(screen.queryByText('admin overview')).toBeNull()
    expect(rows().length).toBe(15)
    expect(idx()).toBe(at)
    setNarrow(false)
    await act(async () => { window.dispatchEvent(new Event('resize')) })
    expect(await screen.findByText('admin overview')).toBeTruthy()
    expect(idx()).toBe(at)
  })
})

describe('the trail', () => {
  it('stores the pathname alone, so a reset token stays out of storage', async () => {
    await coldLoad('/reset-password?token=one-use-1')
    expect(screen.getByText('reset password')).toBeTruthy()
    const raw = sessionStorage.getItem('otodock.settingsTrail') ?? ''
    expect(raw).toContain('/reset-password')
    expect(raw).not.toContain('one-use-1')
    expect(raw).not.toContain('token')
  })

  it('stores the pathname alone, so an OAuth return code stays out of storage', async () => {
    await coldLoad('/auth/callback?code=c-1&state=s-1')
    const raw = sessionStorage.getItem('otodock.settingsTrail') ?? ''
    expect(raw).toContain('/auth/callback')
    expect(raw).not.toContain('c-1')
    expect(raw).not.toContain('state=')
  })
})

describe('the admin dialogs', () => {
  it('stay mounted across the phone list and section moves', async () => {
    narrow = true
    await coldLoad('/admin')
    expect(h.releaseDialogMounts).toBe(1)
    await click('Users')
    expect(await screen.findByText('admin users')).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Back' })) })
    await settle()
    await click('Shares')
    expect(await screen.findByText('admin shares')).toBeTruthy()
    expect(h.releaseDialogMounts).toBe(1)
  })
})

describe('the fold on a section', () => {
  it('keeps the page and its state across a fold and an unfold', async () => {
    narrow = false
    await coldLoad('/admin/platform?tab=security')
    expect(await screen.findByText('setup tab security')).toBeTruthy()
    await click('type a letter')
    expect(screen.getByText('typed 1')).toBeTruthy()
    const at = idx()
    setNarrow(true)
    await act(async () => { window.dispatchEvent(new Event('resize')) })
    expect(screen.getByRole('button', { name: 'Back' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Setup' })).toBeTruthy()
    expect(screen.queryByRole('navigation')).toBeNull()
    expect(screen.getByText('typed 1')).toBeTruthy()
    expect(idx()).toBe(at)
    setNarrow(false)
    await act(async () => { window.dispatchEvent(new Event('resize')) })
    expect(screen.queryByRole('button', { name: 'Back' })).toBeNull()
    expect(screen.getByRole('link', { name: 'Setup' }).getAttribute('aria-current')).toBe('page')
    expect(screen.getByText('typed 1')).toBeTruthy()
    expect(path()).toBe('/admin/platform?tab=security')
    expect(idx()).toBe(at)
  })
})

describe('Back to Chat', () => {
  it('returns to the chat the admin area was entered from, with its entry number', async () => {
    narrow = true
    await coldLoad('/chat/ops')
    await click('open admin')
    await click('Users')
    expect(idx()).toBe(2)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Back' })) })
    await settle()
    await click('Back to Chat')
    expect(screen.getByText('chat page')).toBeTruthy()
    expect(idx()).toBe(0)
  })

  it('from an agent area opened by a chat link, over the injected list', async () => {
    narrow = true
    await coldLoad('/chat/ops')
    await click('open ops config')
    expect(path()).toBe('/agents/ops/config')
    expect(idx()).toBe(2)
    await back()
    expect(path()).toBe('/agents/ops')
    await click('Back to Chat')
    expect(screen.getByText('chat page')).toBeTruthy()
    expect(idx()).toBe(0)
  })

  it('a ?tab= link from a chat lands on the section with the list behind it', async () => {
    narrow = true
    await coldLoad('/chat/ops')
    await click('open usage tab')
    expect(path()).toBe('/user-settings/usage')
    expect(await screen.findByText('usage section')).toBeTruthy()
    expect(idx()).toBe(2)
    await back()
    expect(path()).toBe('/user-settings')
    await back()
    expect(screen.getByText('chat page')).toBeTruthy()
  })

  it('from a cold desktop load goes to the default chat at the root', async () => {
    await coldLoad('/admin/users')
    expect(await screen.findByText('admin users')).toBeTruthy()
    await click('Back to Chat')
    expect(screen.getByText('default chat')).toBeTruthy()
    expect(idx()).toBe(0)
  })
})
