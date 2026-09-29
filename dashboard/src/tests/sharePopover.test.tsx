import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The popover composes the share list, the add form and the link over the
// shares hooks; the hooks are stubbed, the server rules are theirs.
const state = vi.hoisted(() => ({
  shares: [] as unknown[],
  directory: null as unknown[] | null,
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
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: { sub: 'u-me' } }) }))

import SharePopover from '@/components/sharing/SharePopover'
import { ConfirmRequired } from '@/api/shares'
import { savePending, saveConfirm } from '@/lib/shareConfirm'

const app = {
  id: 'app-1', title: 'Schedule', slug: 'schedule', scope: 'personal' as const,
  actions: [{ id: 'book', label: 'Book', type: 'fire_task' as const, task_id: 't1' }],
}

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
  state.directory = null
  state.settings = { max_expiry_days: null }
  state.create.mockReset()
  state.createAsync.mockReset()
  state.patch.mockReset()
  state.patchAsync.mockReset()
  startOidc.mockClear()
  localStorage.clear()
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

  it('shares by a directory pick (the sub) or by what was typed, with the expiry', () => {
    state.directory = [{ sub: 'u9', name: 'Ana Ruiz', username: 'ana' }]
    renderPopover()
    const who = screen.getByLabelText('Who to share with') as HTMLInputElement
    expect(who.placeholder).toBe('Name or username')
    fireEvent.change(who, { target: { value: 'Ana Ruiz' } })
    fireEvent.change(screen.getByLabelText('Expiry'), { target: { value: '30d' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(state.create).toHaveBeenCalledWith(
      { target_kind: 'app', target_id: 'app-1', grantee: 'u9', expires_in: '30d' },
      expect.anything(),
    )
    fireEvent.change(who, { target: { value: 'someone@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(state.create).toHaveBeenLastCalledWith(
      { target_kind: 'app', target_id: 'app-1', grantee: 'someone@example.com', expires_in: '30d' },
      expect.anything(),
    )
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
  it('shares a chat as a snapshot, with the tool calls only when asked', () => {
    state.directory = [{ sub: 'u9', name: 'Ana Ruiz', username: 'ana' }]
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <SharePopover target={{ kind: 'chat', id: 'chat-1', title: 'Plan review', scope: 'personal' }} onClose={vi.fn()} />
      </QueryClientProvider>,
    )
    expect(screen.getByText(/read-only copy of the conversation/)).toBeTruthy()
    // No app link line for a chat.
    expect(screen.queryByText(/\/apps\//)).toBeNull()
    fireEvent.click(screen.getByLabelText('Include tool calls'))
    fireEvent.change(screen.getByLabelText('Who to share with'), { target: { value: 'ana' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(state.create).toHaveBeenCalledWith(
      { target_kind: 'chat', target_id: 'chat-1', grantee: 'u9', expires_in: '', include_tools: true },
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
    expect(localStorage.getItem('otodock-confirm:u-me')).toBeNull()
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
    localStorage.setItem('otodock-confirm:u-me', JSON.stringify({ token: 'old', at: Date.now() - 6 * 60 * 1000 }))
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
