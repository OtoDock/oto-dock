import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { AppSecret, PinnedApp } from '@/api/apps'

// The panel lists the declared secrets and posts values; everything goes
// to the proxy through apiFetch — stubbed here. A value goes out once and
// is never read back.
const calls = vi.hoisted(() => [] as { path: string; method: string; body: string }[])
const listStub = vi.hoisted(() => ({ current: [] as AppSecret[], waiting: '' }))
const triggersStub = vi.hoisted(() => ({ current: [] as Record<string, unknown>[] }))
vi.mock('@/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/auth')>()),
  apiFetch: vi.fn(async (path: string, init?: RequestInit) => {
    calls.push({ path, method: init?.method || 'GET', body: String(init?.body ?? '') })
    if (path.startsWith('/v1/triggers')) {
      return new Response(JSON.stringify({ triggers: triggersStub.current }), { status: 200 })
    }
    if (path.endsWith('/secrets') && (!init?.method || init.method === 'GET')) {
      return new Response(JSON.stringify({ secrets: listStub.current, waiting: listStub.waiting }), { status: 200 })
    }
    if (path.includes('/secrets/') && init?.method === 'PUT') {
      const name = path.split('/secrets/')[1]
      listStub.current = listStub.current.map((s) => (s.name === name ? { ...s, set: true, set_by: 'me' } : s))
      return new Response(JSON.stringify({ status: 'ok', name, restarted: 1 }), { status: 200 })
    }
    if (path.includes('/secrets/') && init?.method === 'DELETE') {
      const name = path.split('/secrets/')[1]
      listStub.current = listStub.current.filter((s) => s.declared !== false || s.name !== name)
        .map((s) => (s.name === name ? { ...s, set: false, set_by: '' } : s))
      return new Response(JSON.stringify({ status: 'ok', name, removed: true, restarted: 1 }), { status: 200 })
    }
    return new Response('{}', { status: 200 })
  }),
}))

import AppSettingsPanel from '@/components/apps/AppSettingsPanel'

function mkApp(over: Partial<PinnedApp> = {}): PinnedApp {
  return {
    id: 'app-1', slug: 'shop', title: 'Shop', scope: 'personal', position: 0,
    rel_path: 'users/alice/workspace/apps/shop', updated_at: 't', kind: 'folder',
    actions: [], actions_sig: 'sig', actions_approved: true, approval_stale: false,
    can_approve: true, can_manage: true, hidden_for_me: false, ...over,
  }
}

const SECRETS: AppSecret[] = [
  { name: 'STRIPE_SECRET_KEY', required: true, description: 'a restricted key', declared: true, set: false,
    sends_to: { host: 'api.example.test', header: 'Authorization', prefix: 'Bearer ' } },
  { name: 'SMTP_PASSWORD', required: false, declared: true, set: true, set_by: 'alice', updated_at: '2026-09-18T05:00:00Z', env: true },
  { name: 'OLD_KEY', required: false, declared: false, set: true, set_by: 'alice' },
]

function renderPanel(app = mkApp(), onClose = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AppSettingsPanel app={app} agent="dev" onClose={onClose} />
    </QueryClientProvider>,
  )
  return onClose
}

describe('AppSettingsPanel', () => {
  beforeEach(() => {
    calls.length = 0
    listStub.current = SECRETS.map((s) => ({ ...s }))
    listStub.waiting = 'STRIPE_SECRET_KEY is not set'
  })

  it('lists every secret with where its value goes and whether it is set, never a value', async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByTestId('secret-row-STRIPE_SECRET_KEY')).toBeTruthy())
    const key = screen.getByTestId('secret-row-STRIPE_SECRET_KEY').textContent || ''
    expect(key).toContain('required')
    expect(key).toContain('goes to api.example.test only')
    expect(key).toContain('a restricted key')
    // A short description has no fold.
    expect(screen.getByTestId('secret-row-STRIPE_SECRET_KEY').querySelector('[data-testid="secret-description-more"]')).toBeNull()
    expect(screen.getByTestId('secret-state-STRIPE_SECRET_KEY').textContent).toBe('not set — required')
    const smtp = screen.getByTestId('secret-row-SMTP_PASSWORD').textContent || ''
    expect(smtp).toContain('read by the server itself')
    expect(screen.getByTestId('secret-state-SMTP_PASSWORD').textContent).toContain('set by alice')
    const old = screen.getByTestId('secret-row-OLD_KEY')
    expect(old.textContent).toContain('no longer declared by the manifest')
    expect(old.querySelector('input')).toBeNull()
    expect(old.textContent).toContain('Remove')
    // The inputs are password fields with no value; a set one says so.
    const input = screen.getByLabelText('Value of SMTP_PASSWORD') as HTMLInputElement
    expect(input.type).toBe('password')
    expect(input.value).toBe('')
    expect(input.placeholder).toContain('(set)')
    expect(calls.filter((c) => c.method === 'GET' && c.path.endsWith('/secrets'))).toHaveLength(1)
  })

  it('saves a typed value once, clears the field and says the server picks it up', async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByLabelText('Value of STRIPE_SECRET_KEY')).toBeTruthy())
    const input = screen.getByLabelText('Value of STRIPE_SECRET_KEY') as HTMLInputElement
    const save = screen.getByTestId('secret-row-STRIPE_SECRET_KEY').querySelector('button') as HTMLButtonElement
    expect(save.textContent).toBe('Save')
    expect(save.disabled).toBe(true)
    fireEvent.change(input, { target: { value: 'sk-test-alpha-beta' } })
    expect(save.disabled).toBe(false)
    fireEvent.click(save)
    await waitFor(() => expect(screen.getByTestId('settings-notice').textContent).toContain('STRIPE_SECRET_KEY is set'))
    const put = calls.find((c) => c.method === 'PUT')
    expect(put?.path).toBe('/v1/apps/app-1/secrets/STRIPE_SECRET_KEY')
    expect(JSON.parse(put!.body)).toEqual({ value: 'sk-test-alpha-beta' })
    expect(input.value).toBe('')
    await waitFor(() => expect(screen.getByTestId('secret-state-STRIPE_SECRET_KEY').textContent).toContain('set by me'))
    // Enter in the field saves too.
    fireEvent.change(input, { target: { value: 'sk-test-gamma' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(2))
  })

  it('removes a value behind a confirm, shows the proxy\'s refusal, and closes on Escape', async () => {
    const onClose = renderPanel()
    await waitFor(() => expect(screen.getByTestId('secret-row-OLD_KEY')).toBeTruthy())
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(screen.getByTestId('secret-row-OLD_KEY').querySelector('button') as HTMLButtonElement)
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0)
    confirm.mockReturnValue(true)
    fireEvent.click(screen.getByTestId('secret-row-OLD_KEY').querySelector('button') as HTMLButtonElement)
    await waitFor(() => expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(1))
    expect(calls.find((c) => c.method === 'DELETE')?.path).toBe('/v1/apps/app-1/secrets/OLD_KEY')
    await waitFor(() => expect(screen.queryByTestId('secret-row-OLD_KEY')).toBeNull())
    confirm.mockRestore()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  // Found on the VM pass 2026-09-18: an agent wrote a paragraph per secret
  // and the panel on a phone was all text — a long description is clamped
  // to two lines behind "more"; the platform's own words are one line.
  it('clamps a long description behind "more" and keeps its own words to a line', async () => {
    const long = 'the signing secret of the Stripe webhook endpoint pointed at this app’s inbound address (Developers → Webhooks; send checkout.session.completed and checkout.session.expired)'
    listStub.current = [{ name: 'STRIPE_WEBHOOK_SECRET', required: true, declared: true, set: false, description: long }]
    renderPanel()
    await waitFor(() => expect(screen.getByTestId('secret-description')).toBeTruthy())
    const desc = screen.getByTestId('secret-description')
    expect(desc.querySelector('span')?.className).toContain('line-clamp-2')
    const more = screen.getByTestId('secret-description-more')
    expect(more.textContent).toBe('more')
    fireEvent.click(more)
    expect(desc.querySelector('span')?.className).not.toContain('line-clamp-2')
    expect(screen.getByTestId('secret-description-more').textContent).toBe('less')
    const panel = screen.getByTestId('app-settings-panel').textContent || ''
    expect(panel).toContain('A value is typed here by a person and never shown again.')
    expect(panel).not.toContain('never by the agent')
  })

  it('says when the app declares no secrets', async () => {
    listStub.current = []
    renderPanel()
    await waitFor(() => expect(screen.getByText('This app declares no secrets.')).toBeTruthy())
  })

  // APPS.md "Inbound hooks": each hook's address to paste into the vendor,
  // with a Copy button, the scheme in words, the secret's state and the
  // handler it wakes.
  it('shows each inbound hook\'s address with Copy, its scheme, its secret\'s state and its handler', async () => {
    listStub.current = [
      { name: 'STRIPE_WEBHOOK_SECRET', required: true, declared: true, set: false },
      { name: 'GH_SECRET', required: true, declared: true, set: true, set_by: 'alice' },
    ]
    const written: string[] = []
    Object.assign(navigator, { clipboard: { writeText: async (t: string) => { written.push(t) } } })
    renderPanel(mkApp({
      inbound: {
        stripe: { verify: 'stripe', secret: 'STRIPE_WEBHOOK_SECRET', handler: 'payment' },
        gh: { verify: 'github', secret: 'GH_SECRET', handler: 'push' },
      },
    }))
    await waitFor(() => expect(screen.getByTestId('inbound-row-stripe')).toBeTruthy())
    const stripe = screen.getByTestId('inbound-row-stripe')
    expect(stripe.textContent).toContain('Stripe-signed events (the Stripe-Signature header)')
    expect(stripe.textContent).toContain(`${window.location.origin}/v1/apps/app-1/inbound/stripe`)
    // The secrets' states arrive with the listing.
    await waitFor(() => expect(stripe.textContent).toContain('verified with the secret STRIPE_WEBHOOK_SECRET (not set — the address is off until it is)'))
    expect(stripe.textContent).toContain('(not set — the address is off until it is), and wakes payment')
    expect(screen.getByTestId('inbound-section').textContent).toContain('Paste the address into the vendor’s webhook settings. Its signing secret goes above.')
    const gh = screen.getByTestId('inbound-row-gh')
    expect(gh.textContent).toContain('GitHub-signed events')
    expect(gh.textContent).toContain('GH_SECRET (set)')
    fireEvent.click(stripe.querySelector('button') as HTMLButtonElement)
    await waitFor(() => expect(written).toEqual([`${window.location.origin}/v1/apps/app-1/inbound/stripe`]))
    await waitFor(() => expect(stripe.textContent).toContain('Copied'))
  })

  // The triggers aimed at the app (a template's blueprint seeds one per
  // copy): the address each is fired at with Copy, the key it takes by
  // scope, a paused one said so, a subscription-fed one without an address.
  it('lists the triggers aimed at the app with their address and the key they take', async () => {
    listStub.current = []
    triggersStub.current = [
      { id: 't1', slug: 'home-ping', name: 'Ping', scope: 'user', agent: 'dev', created_by: 'alice', app_id: 'app-1',
        handler: 'ping', enabled: true, last_error: '', subscription_id: null, webhook_path: '/v1/webhooks/user/alice/home-ping' },
      { id: 't2', slug: 'home-mail', name: 'Mail', scope: 'user', agent: 'dev', created_by: 'alice', app_id: 'app-1',
        handler: 'mail', enabled: false, last_error: 'paused: removed from the agent', subscription_id: 'sub-1', webhook_path: null },
      { id: 't3', slug: 'other', name: 'Other', scope: 'user', agent: 'dev', created_by: 'alice', app_id: 'app-9',
        handler: 'x', enabled: true, last_error: '', subscription_id: null, webhook_path: '/v1/webhooks/user/alice/other' },
    ]
    const written: string[] = []
    Object.assign(navigator, { clipboard: { writeText: async (t: string) => { written.push(t) } } })
    renderPanel(mkApp())
    await waitFor(() => expect(screen.getByTestId('triggers-section')).toBeTruthy())
    const section = screen.getByTestId('triggers-section')
    expect(section.textContent).toContain('POST to the address with your API key (User Settings → Integrations)')
    const ping = screen.getByTestId('trigger-row-home-ping')
    expect(ping.textContent).toContain('wakes ping')
    expect(ping.textContent).toContain(`${window.location.origin}/v1/webhooks/user/alice/home-ping`)
    fireEvent.click(ping.querySelector('button') as HTMLButtonElement)
    await waitFor(() => expect(written).toEqual([`${window.location.origin}/v1/webhooks/user/alice/home-ping`]))
    const mail = screen.getByTestId('trigger-row-home-mail')
    expect(mail.textContent).toContain('paused')
    expect(mail.textContent).toContain('fed by a vendor subscription')
    expect(mail.textContent).toContain('paused: removed from the agent')
    expect(screen.queryByTestId('trigger-row-other')).toBeNull()
  })

  it('says a shared app takes an agent API key', async () => {
    listStub.current = []
    triggersStub.current = [
      { id: 't4', slug: 'board-ping', name: 'Ping', scope: 'agent', agent: 'dev', created_by: 'dev', app_id: 'app-1',
        handler: 'ping', enabled: true, last_error: '', subscription_id: null, webhook_path: '/v1/webhooks/agent/dev/board-ping' },
    ]
    renderPanel(mkApp({ scope: 'shared', rel_path: 'workspace/apps/board' }))
    await waitFor(() => expect(screen.getByTestId('triggers-section')).toBeTruthy())
    expect(screen.getByTestId('triggers-section').textContent).toContain('with an agent API key (the Triggers tab)')
  })
})
