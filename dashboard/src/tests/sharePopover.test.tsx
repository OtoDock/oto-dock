import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The popover composes the share list, the add form and the link over the
// shares hooks; the hooks are stubbed, the server rules are theirs.
type Directory = { users: unknown[] | null; agents: unknown[]; departments: unknown[] }
const closed = (): Directory => ({ users: null, agents: [], departments: [] })
const state = vi.hoisted(() => ({
  shares: [] as unknown[],
  directory: { users: null, agents: [], departments: [] } as Directory | undefined,
  settings: { max_expiry_days: null } as { max_expiry_days: number | null } | undefined,
  create: vi.fn(),
  createAsync: vi.fn(),
  patch: vi.fn(),
  patchAsync: vi.fn(),
}))
vi.mock('@/api/shares', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/shares')>()),
  useShares: () => ({ data: state.shares, isLoading: false, error: null }),
  useUserDirectory: () => ({ data: state.directory }),
  useSharingSettings: () => ({ data: state.settings }),
  useCreateShare: () => ({ mutate: state.create, mutateAsync: state.createAsync, isPending: false, error: null }),
  usePatchShare: () => ({ mutate: state.patch, mutateAsync: state.patchAsync, isPending: false, error: null }),
}))
vi.mock('@/api/webauthn', () => ({ passkeyConfirm: vi.fn(async () => 'pk-token') }))
// The identity-provider round trip leaves the page; the start is stubbed.
const startOidc = vi.hoisted(() => vi.fn(async () => {}))
vi.mock('@/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/auth')>()),
  startOidcConfirm: startOidc,
}))
const authUser = vi.hoisted(() => ({ current: { sub: 'u-me', role: 'member', agents: ['dev', 'ops'], agent_roles: { dev: 'editor', ops: 'editor' } } as Record<string, unknown> }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: authUser.current }) }))

import SharePopover from '@/components/sharing/SharePopover'
import { ConfirmRequired } from '@/api/shares'
import { savePending, saveConfirm } from '@/lib/shareConfirm'

const app = {
  id: 'app-1', title: 'Schedule', slug: 'schedule', scope: 'personal' as const, agent: 'dev',
  actions: [{ id: 'book', label: 'Book', type: 'fire_task' as const, task_id: 't1' }],
}
const teamApp = { ...app, id: 'app-2', title: 'Board', slug: 'board', scope: 'shared' as const }
const directoryWithAll = (): Directory => ({
  users: [{ sub: 'u9', name: 'Ana Ruiz', username: 'ana' }],
  agents: [{ slug: 'dev', display_name: 'Dev', color: '' }, { slug: 'ops', display_name: 'Ops Desk', color: '' }],
  departments: [{ id: 'dept-1', name: 'Engineering' }],
})

function renderPopover(onClose = vi.fn(), extra: { initialTab?: 'people' | 'link'; confirmError?: string } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SharePopover app={app} onClose={onClose} {...extra} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  state.shares = []
  state.directory = closed()
  state.settings = { max_expiry_days: null }
  authUser.current = { sub: 'u-me', role: 'member', agents: ['dev', 'ops'], agent_roles: { dev: 'editor', ops: 'editor' } }
  state.create.mockReset()
  state.createAsync.mockReset()
  state.patch.mockReset()
  state.patchAsync.mockReset()
  startOidc.mockClear()
  localStorage.clear()
  sessionStorage.clear()
})

const openLinkTab = () => fireEvent.click(screen.getByRole('button', { name: 'Link' }))
const flush = () => new Promise((r) => setTimeout(r, 0))

describe('SharePopover', () => {
  it('tells a link maker that an app with steps treats a visitor’s input as data', () => {
    // APPS.md "Steps": the warning reaches a link made after the approval.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <SharePopover app={{ ...app, steps: { sync: { run: 'scripts/sync.sh' } } }} onClose={vi.fn()} />
      </QueryClientProvider>,
    )
    openLinkTab()
    expect(screen.getByTestId('share-steps-note').textContent).toContain('never instructions')
    document.body.innerHTML = ''
    renderPopover()
    openLinkTab()
    expect(screen.queryByTestId('share-steps-note')).toBeNull()
  })

  it('lists who has the app, removes and resumes through the share', () => {
    state.shares = [
      { id: 's1', scope: 'internal', state: 'active', grantee: { sub: 'u1', name: 'Nora', username: 'nora' }, expires_at: null },
      { id: 's2', scope: 'internal', state: 'suspended', grantee: { sub: 'u2', name: 'Kim', username: 'kim' }, expires_at: null },
    ]
    renderPopover()
    expect(screen.getByText('Nora')).toBeTruthy()
    expect(screen.getByText('paused while the app is unpinned')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Remove Nora'))
    expect(state.patch).toHaveBeenCalledWith({ id: 's1', revoke: true }, expect.anything())
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    expect(state.patch).toHaveBeenCalledWith({ id: 's2', resume: true }, expect.anything())
  })

  it('shares by a directory pick (the sub) or by what was typed, with the expiry and the role', () => {
    state.directory = { ...directoryWithAll(), agents: [], departments: [] }
    renderPopover()
    const who = screen.getByLabelText('Who to share with') as HTMLInputElement
    expect(who.placeholder).toBe('Name or username')
    // A personal app offers people only, so no "Share with" picker; the
    // owner is its manager, so every cap is on offer, viewer first.
    expect(screen.queryByLabelText('Share with')).toBeNull()
    expect(Array.from((screen.getByLabelText('Role') as HTMLSelectElement).options).map((o) => o.value))
      .toEqual(['viewer', 'contributor', 'editor', 'manager'])
    fireEvent.change(who, { target: { value: 'Ana Ruiz' } })
    fireEvent.change(screen.getByLabelText('Expiry'), { target: { value: '30d' } })
    fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'editor' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(state.create).toHaveBeenCalledWith(
      { target_kind: 'app', target_id: 'app-1', grantee_kind: 'person', grantee: 'u9', expires_in: '30d', role_cap: 'editor' },
      expect.anything(),
    )
    fireEvent.change(who, { target: { value: 'someone@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(state.create).toHaveBeenLastCalledWith(
      { target_kind: 'app', target_id: 'app-1', grantee_kind: 'person', grantee: 'someone@example.com', expires_in: '30d', role_cap: 'editor' },
      expect.anything(),
    )
    // The notice names what the share did.
    act(() => { state.create.mock.calls[1][1].onSuccess({ status: 'ok', share: { target_kind: 'app', grantee: { name: 'Ana Ruiz' }, to_agent: null, to_department: null } }) })
    expect(screen.getByText('Waiting for Ana Ruiz to accept.')).toBeTruthy()
  })

  it('a team app offers an agent and, to an admin, a department; the caps stop at my role', () => {
    // The server lists departments to an admin only; an editor's directory
    // carries none.
    state.directory = { ...directoryWithAll(), departments: [] }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = render(
      <QueryClientProvider client={qc}>
        <SharePopover app={teamApp} onClose={vi.fn()} />
      </QueryClientProvider>,
    )
    const withSel = screen.getByLabelText('Share with') as HTMLSelectElement
    // An editor: no department option, caps up to editor; the app's own
    // agent is not a place for it.
    expect(Array.from(withSel.options).map((o) => o.value)).toEqual(['person', 'agent'])
    expect(Array.from((screen.getByLabelText('Role') as HTMLSelectElement).options).map((o) => o.value))
      .toEqual(['viewer', 'contributor', 'editor'])
    fireEvent.change(withSel, { target: { value: 'agent' } })
    const agentSel = screen.getByLabelText('Which agent') as HTMLSelectElement
    expect(Array.from(agentSel.options).map((o) => o.value)).toEqual(['', 'ops'])
    expect((screen.getByRole('button', { name: 'Share' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(agentSel, { target: { value: 'ops' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(state.create).toHaveBeenLastCalledWith(
      { target_kind: 'app', target_id: 'app-2', grantee_kind: 'agent', grantee: 'ops', expires_in: '', role_cap: 'viewer' },
      expect.anything(),
    )
    act(() => { state.create.mock.calls[0][1].onSuccess({ status: 'ok', share: { target_kind: 'app', grantee: null, to_agent: { slug: 'ops', name: 'Ops Desk' }, to_department: null } }) })
    expect(screen.getByText("Added to Ops Desk's apps.")).toBeTruthy()
    unmount()
    // An admin: the department option too, every cap.
    authUser.current = { sub: 'u-me', role: 'admin', agents: [], agent_roles: {} }
    state.directory = directoryWithAll()
    render(
      <QueryClientProvider client={qc}>
        <SharePopover app={teamApp} onClose={vi.fn()} />
      </QueryClientProvider>,
    )
    const sel = screen.getByLabelText('Share with') as HTMLSelectElement
    expect(Array.from(sel.options).map((o) => o.value)).toEqual(['person', 'agent', 'department'])
    fireEvent.change(sel, { target: { value: 'department' } })
    fireEvent.change(screen.getByLabelText('Which department'), { target: { value: 'dept-1' } })
    fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'manager' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(state.create).toHaveBeenLastCalledWith(
      { target_kind: 'app', target_id: 'app-2', grantee_kind: 'department', grantee: 'dept-1', expires_in: '', role_cap: 'manager' },
      expect.anything(),
    )
  })

  it('lists agent and department shares with their state, and a declined person may be shared again', () => {
    state.directory = directoryWithAll()
    state.shares = [
      { id: 'a1', scope: 'internal', state: 'active', grantee: null, to_agent: { slug: 'ops', name: 'Ops Desk' }, to_department: null, role_cap: 'editor', decision: 'accepted', expires_at: null },
      { id: 'd1', scope: 'internal', state: 'active', grantee: null, to_agent: null, to_department: { id: 'dept-1', name: 'Engineering' }, role_cap: 'viewer', decision: 'accepted', expires_at: null },
      { id: 'p1', scope: 'internal', state: 'active', grantee: { sub: 'u9', name: 'Ana Ruiz', username: 'ana' }, to_agent: null, to_department: null, role_cap: 'viewer', decision: 'declined', decided_by_name: 'Ana Ruiz', expires_at: null },
      { id: 'p2', scope: 'internal', state: 'active', target_kind: 'app', grantee: { sub: 'u8', name: 'Bo Lind', username: 'bo' }, to_agent: null, to_department: null, role_cap: 'editor', decision: 'accepted', placed_agent: 'lite', expires_at: null },
    ]
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <SharePopover app={teamApp} onClose={vi.fn()} />
      </QueryClientProvider>,
    )
    const list = screen.getByTestId('share-list')
    expect(list.textContent).toContain('Agent: Ops Desk')
    expect(list.textContent).toContain('editor')
    expect(list.textContent).toContain('Department: Engineering')
    expect(list.textContent).toContain('declined by Ana Ruiz')
    expect(list.textContent).toContain('accepted')
    expect(list.textContent).not.toContain('lite')
    // Ana is still a candidate: the datalist offers her again.
    expect(screen.getByText('ana').tagName.toLowerCase()).toBe('option')
    fireEvent.click(screen.getByLabelText('Remove Ops Desk'))
    expect(state.patch).toHaveBeenCalledWith({ id: 'a1', revoke: true }, expect.anything())
  })

  it('lists people and links together under either tab', () => {
    state.shares = [
      { id: 's1', scope: 'internal', state: 'active', grantee: { sub: 'u1', name: 'Nora', username: 'nora' }, expires_at: null },
      { id: 'l1', scope: 'external', state: 'active', public: false, access_count: 2, expires_at: null },
    ]
    renderPopover()
    const list = screen.getByTestId('share-list')
    expect(list.textContent).toContain('Nora')
    expect(list.textContent).toContain('opened 2×')
    openLinkTab()
    expect(screen.getByTestId('share-list').textContent).toContain('Nora')
    expect(screen.getByLabelText('Revoke link l1')).toBeTruthy()
    expect(screen.queryByText('No links yet.')).toBeNull()
  })

  it('takes an exact username or email when the directory is closed and shows the link', () => {
    state.directory = closed()
    renderPopover()
    expect((screen.getByLabelText('Who to share with') as HTMLInputElement).placeholder).toBe('Exact username or email')
    expect(screen.getByText(/\/apps\/app-1$/)).toBeTruthy()
    expect(screen.getByText('Nobody yet.')).toBeTruthy()
  })

  it('closes from its button and from the backdrop', () => {
    const onClose = vi.fn()
    renderPopover(onClose)
    fireEvent.click(screen.getByLabelText('Close'))
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('presentation'))
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})

describe('SharePopover — chats', () => {
  it('shares a chat as a snapshot, with the tool calls only when asked, people only and no role', () => {
    state.directory = directoryWithAll()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <SharePopover target={{ kind: 'chat', id: 'chat-1', title: 'Plan review', scope: 'personal' }} onClose={vi.fn()} />
      </QueryClientProvider>,
    )
    expect(screen.getByText(/read-only copy of the conversation/)).toBeTruthy()
    // No app link line for a chat, no agent or department target, no role.
    expect(screen.queryByText(/\/apps\//)).toBeNull()
    expect(screen.queryByLabelText('Share with')).toBeNull()
    expect(screen.queryByLabelText('Role')).toBeNull()
    fireEvent.click(screen.getByLabelText('Include tool calls'))
    fireEvent.change(screen.getByLabelText('Who to share with'), { target: { value: 'ana' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(state.create).toHaveBeenCalledWith(
      { target_kind: 'chat', target_id: 'chat-1', grantee_kind: 'person', grantee: 'u9', expires_in: '', include_tools: true },
      expect.anything(),
    )
  })
})

describe('SharePopover — links', () => {
  it('makes a password link after the confirm and shows the secret once', async () => {
    state.createAsync
      .mockRejectedValueOnce(new ConfirmRequired('password', 'Enter your password'))
      .mockResolvedValueOnce({ status: 'ok', link: 'https://x/s/tok', password: 'abc23456', share: { id: 'l1' } })
    renderPopover()
    openLinkTab()
    // Buttons on the link names the actions and, on a personal app, whose accounts they use.
    expect(screen.getByLabelText('Buttons on the link')).toBeTruthy()
    expect(screen.getByText(/connected accounts, as you/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Make link' }))
    await flush()
    expect(state.createAsync).toHaveBeenLastCalledWith(expect.objectContaining({
      scope: 'external', public: false, allow_actions: false, expires_in: '30d',
    }))
    // The server asked for the password: the form appears and the call repeats with it.
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'pw-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await flush()
    expect(state.createAsync).toHaveBeenLastCalledWith(expect.objectContaining({ password: 'pw-1' }))
    expect(screen.getByText('abc23456')).toBeTruthy()
    expect(screen.getByText('https://x/s/tok')).toBeTruthy()
  })

  it('a public link needs the understanding checkbox; a passkey confirm runs by itself', async () => {
    state.createAsync
      .mockRejectedValueOnce(new ConfirmRequired('passkey', 'Confirm with your passkey'))
      .mockResolvedValueOnce({ status: 'ok', link: 'https://x/s/pub', share: { id: 'l2' } })
    renderPopover()
    openLinkTab()
    fireEvent.click(screen.getByLabelText(/^Public/))
    const make = screen.getByRole('button', { name: 'Make link' }) as HTMLButtonElement
    expect(make.disabled).toBe(true)
    fireEvent.click(screen.getByLabelText(/I understand anyone/))
    expect(make.disabled).toBe(false)
    fireEvent.click(make)
    await flush()
    await flush()
    expect(state.createAsync).toHaveBeenLastCalledWith(expect.objectContaining({ public: true, confirm_token: 'pk-token' }))
    expect(screen.getByText('https://x/s/pub')).toBeTruthy()
    expect(screen.queryByText(/Password/)).toBeNull()
  })

  it('a link offers "Never expires" only while no admin cap is set, and sends "never"', async () => {
    state.createAsync.mockResolvedValueOnce({ status: 'ok', link: 'https://x/s/qr', share: { id: 'l4' } })
    const linkOptions = () => Array.from((screen.getByLabelText('Link expiry') as HTMLSelectElement).options)
    renderPopover()
    openLinkTab()
    expect(linkOptions().map((o) => o.value)).toEqual(['7d', '30d', '90d', 'never'])
    // The default stays a duration: an unchanged form never makes a forever link.
    expect((screen.getByLabelText('Link expiry') as HTMLSelectElement).value).toBe('30d')
    fireEvent.change(screen.getByLabelText('Link expiry'), { target: { value: 'never' } })
    fireEvent.click(screen.getByLabelText(/^Public/))
    // A public link that never expires still asks for the understanding.
    expect((screen.getByRole('button', { name: 'Make link' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByLabelText(/I understand anyone/))
    fireEvent.click(screen.getByRole('button', { name: 'Make link' }))
    await flush()
    expect(state.createAsync).toHaveBeenLastCalledWith(expect.objectContaining({ public: true, expires_in: 'never' }))
  })

  it('a capped or still-loading expiry setting offers no "Never" on a link', () => {
    state.settings = { max_expiry_days: 30 }
    const { unmount } = renderPopover()
    openLinkTab()
    const values = () => Array.from((screen.getByLabelText('Link expiry') as HTMLSelectElement).options).map((o) => o.value)
    expect(values()).toEqual(['7d', '30d', '90d'])
    unmount()
    state.settings = undefined
    renderPopover()
    openLinkTab()
    expect(values()).toEqual(['7d', '30d', '90d'])
  })

  it('an identity-provider account is sent to the provider with its choices kept', async () => {
    state.createAsync.mockRejectedValueOnce(new ConfirmRequired('oidc', 'Confirm with Mock SSO', 'Mock SSO'))
    renderPopover()
    openLinkTab()
    fireEvent.change(screen.getByLabelText('Link expiry'), { target: { value: '7d' } })
    fireEvent.click(screen.getByLabelText('Buttons on the link'))
    fireEvent.click(screen.getByRole('button', { name: 'Make link' }))
    await flush()
    expect(screen.getByText(/goes to Mock SSO to check it is you/)).toBeTruthy()
    // Cancel keeps nothing.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(localStorage.getItem('otodock-share-pending:u-me')).toBeNull()
    expect(startOidc).not.toHaveBeenCalled()
    // Continue: the write waits under the account and the page leaves.
    state.createAsync.mockRejectedValueOnce(new ConfirmRequired('oidc', 'Confirm with Mock SSO', 'Mock SSO'))
    fireEvent.click(screen.getByRole('button', { name: 'Make link' }))
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'Continue to Mock SSO' }))
    expect(startOidc).toHaveBeenCalledWith('/apps/app-1?share=1&tab=link')
    const pending = JSON.parse(localStorage.getItem('otodock-share-pending:u-me') || '{}')
    expect(pending).toMatchObject({
      app_id: 'app-1', op: 'create', tab: 'link', return_to: '/apps/app-1?share=1&tab=link',
      fields: { public: false, expiry: '7d', buttons: true, include_tools: false },
    })
    expect(JSON.stringify(pending)).not.toContain('password')
  })

  it('back from the provider: the choices are restored, the token arms the click, nothing fires by itself', async () => {
    savePending('u-me', {
      app_id: 'app-1', target_kind: 'app', op: 'create', tab: 'link', return_to: '/apps/app-1?share=1&tab=link',
      fields: { public: true, expiry: '90d', buttons: false, include_tools: false },
    })
    saveConfirm('u-me', 'oc-1')
    state.createAsync.mockResolvedValueOnce({ status: 'ok', link: 'https://x/s/pub', share: { id: 'l3' } })
    renderPopover(vi.fn(), { initialTab: 'link' })
    expect(screen.getByRole('status').textContent).toBe('Confirmed. Make the link now.')
    expect((screen.getByLabelText(/^Public/) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText('Link expiry') as HTMLSelectElement).value).toBe('90d')
    expect(state.createAsync).not.toHaveBeenCalled()
    // The entries were taken: a second popover finds nothing.
    expect(localStorage.getItem('otodock-share-pending:u-me')).toBeNull()
    expect(sessionStorage.getItem('otodock-confirm:u-me')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Make link' }))
    await flush()
    expect(state.createAsync).toHaveBeenCalledWith(expect.objectContaining({
      public: true, expires_in: '90d', confirm_token: 'oc-1',
    }))
    expect(screen.getByText('https://x/s/pub')).toBeTruthy()
  })

  it('a late return or a refused confirm says so instead of arming anything', () => {
    savePending('u-me', {
      app_id: 'app-1', target_kind: 'app', op: 'create', tab: 'link', return_to: '/apps/app-1?share=1&tab=link',
      fields: { public: false, expiry: '30d', buttons: false, include_tools: false },
    })
    // The token is stale.
    sessionStorage.setItem('otodock-confirm:u-me', JSON.stringify({ token: 'old', at: Date.now() - 6 * 60 * 1000 }))
    const { unmount } = renderPopover(vi.fn(), { initialTab: 'link' })
    expect(screen.getByRole('alert').textContent).toContain('took too long')
    unmount()
    // The provider refused: the reason lands on the form.
    savePending('u-me', {
      app_id: 'app-1', target_kind: 'app', op: 'create', tab: 'link', return_to: '/apps/app-1?share=1&tab=link',
      fields: { public: false, expiry: '30d', buttons: false, include_tools: false },
    })
    renderPopover(vi.fn(), { initialTab: 'link', confirmError: 'The login was not fresh' })
    expect(screen.getByRole('alert').textContent).toContain('The login was not fresh')
    // A pending entry for ANOTHER app is left where it is.
    savePending('u-me', {
      app_id: 'app-9', target_kind: 'app', op: 'create', tab: 'link', return_to: '/apps/app-9?share=1&tab=link',
      fields: { public: false, expiry: '30d', buttons: false, include_tools: false },
    })
    renderPopover(vi.fn(), { initialTab: 'link' })
    expect(JSON.parse(localStorage.getItem('otodock-share-pending:u-me') || '{}').app_id).toBe('app-9')
  })

  it('lists links with their protection, toggles Buttons through the confirm and revokes', async () => {
    state.shares = [
      { id: 'l1', scope: 'external', state: 'active', grantee: null, public: false, allow_actions: false, access_count: 3, expires_at: null },
    ]
    state.patchAsync.mockResolvedValue({})
    renderPopover()
    openLinkTab()
    expect(screen.getByText(/password · opened 3×/)).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Buttons on link l1'))
    await flush()
    expect(state.patchAsync).toHaveBeenCalledWith({ id: 'l1', allow_actions: true })
    fireEvent.click(screen.getByLabelText('Revoke link l1'))
    expect(state.patch).toHaveBeenCalledWith({ id: 'l1', revoke: true }, expect.anything())
  })
})
