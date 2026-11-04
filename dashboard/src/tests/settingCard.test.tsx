// The settings card in the chat (components/chat/SettingCard): every viewer
// reads their own value with their own sign-in, changes it and saves, the
// save echoes, whoever's chat it is; a refusal toasts and puts the control
// back; whether the viewer changes the row is the server's answer, whose
// sentence shows in the control's place (an admin's row for a member), and
// a line shows while it loads or when it did not load; a row the catalog no
// longer has shows one sentence; a device pick saves at once in this
// browser; an admin's row saves behind a confirm; a setting the operator
// manages shows a line; a text saves trimmed and never blank; the line
// under the label names the page; no call carries a token of the chat.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { personClick, renderWithDialogs } from './dialogsHarness'
import * as authApi from '@/api/auth'
import { CATALOG } from '@/components/settings/catalog'

const h = vi.hoisted(() => ({
  user: { current: { sub: 'u1', role: 'member', display_name: 'Dev', agent_roles: {} } as Record<string, unknown> },
  // The server's answer for this viewer, when a test sets one.
  answer: { current: null as null | { status: number; detail?: string } },
  toast: vi.fn(),
  theme: { current: 'system', set: vi.fn() },
}))

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: h.user.current, setUser: vi.fn() }) }))
vi.mock('@/lib/appToasts', () => ({ emitAppToast: h.toast, onAppToast: () => () => {} }))
vi.mock('@/contexts/useTheme', () => ({ useTheme: () => ({ theme: h.theme.current, setTheme: h.theme.set, resolvedTheme: 'light' }) }))

import SettingCard from '@/components/chat/SettingCard'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const puts: unknown[] = []

/** The card's answer as the proxy gives it: an admin's row refuses a viewer
 *  below admin, every other row is theirs to change. */
function cardAnswer(path: string): Response {
  const id = decodeURIComponent(path.slice('/v1/settings/card/'.length))
  const row = CATALOG.find((e) => e.id === id)
  const role = String(h.user.current.role)
  const fixed = h.answer.current
  const answer = fixed ?? (row?.writes === 'admin' && role !== 'admin'
    ? { status: 403, detail: 'An administrator changes this setting.' } : { status: 200 })
  if (answer.status !== 200) {
    const body = answer.detail === undefined ? {} : { detail: answer.detail }
    return { ok: false, status: answer.status, json: async () => body, text: async () => JSON.stringify(body) } as Response
  }
  return { ok: true, status: 200, json: async () => ({ setting: id, label: row?.label, changes: true }) } as Response
}

function mockPrefs(stored: Record<string, unknown>, putStatus = 200) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.startsWith('/v1/settings/card/')) return cardAnswer(path)
    if (path.startsWith('/v1/users/me/ui-prefs') || path.startsWith('/v1/users/me/audio-prefs') || path.startsWith('/v1/admin/platform-settings')) {
      if (init?.method === 'PUT') {
        puts.push(JSON.parse(String(init.body)))
        if (putStatus !== 200) return { ok: false, status: putStatus, json: async () => ({ detail: 'Refused.' }), text: async () => JSON.stringify({ detail: 'Refused.' }) } as Response
        Object.assign(stored, JSON.parse(String(init.body)))
        return { ok: true, status: 200, json: async () => stored } as Response
      }
      return { ok: true, status: 200, json: async () => stored } as Response
    }
    if (path.startsWith('/v1/users/me/profile') && init?.method === 'PUT') puts.push(JSON.parse(String(init.body)))
    return { ok: true, status: 200, json: async () => ({}) } as Response
  })
}

const ALERTS = 'user.general.subscription-usage-alerts'

function card(id = ALERTS) {
  return renderWithDialogs(<SettingCard settingId={id} label="Subscription usage alerts" catalogVersion={1} />, { wrapper })
}

afterEach(() => {
  fetchSpy.mockReset()
  h.toast.mockClear()
  h.theme.set.mockClear()
  puts.length = 0
  h.user.current = { sub: 'u1', role: 'member', display_name: 'Dev', agent_roles: {} }
  h.answer.current = null
})

describe('the settings card', () => {
  it('reads the viewer own value, saves the change and echoes it', async () => {
    mockPrefs({ subscription_usage_alerts: false })
    card()
    const toggle = await screen.findByRole('switch', { name: 'Subscription usage alerts' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(save).toBeEnabled()
    await act(async () => { fireEvent.click(save) })
    await waitFor(() => expect(puts).toEqual([{ subscription_usage_alerts: true }]))
    await screen.findByText('Saved')
    expect(h.toast).not.toHaveBeenCalled()
  })

  it('a refusal toasts and puts the control back', async () => {
    mockPrefs({ subscription_usage_alerts: true }, 403)
    card()
    const toggle = await screen.findByRole('switch', { name: 'Subscription usage alerts' })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(toggle)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    await waitFor(() => expect(h.toast).toHaveBeenCalled())
    expect(h.toast.mock.calls[0][0].title).toBe('The setting was not saved')
    expect(h.toast.mock.calls[0][0].body).toBe('Refused.')
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
  })

  it('serves any viewer of the chat with their own sign-in and no token of the chat', async () => {
    // A viewer who did not ask for the card, in a chat that is not theirs.
    h.user.current = { sub: 'u2', role: 'member', display_name: 'Pat', agent_roles: {} }
    mockPrefs({ subscription_usage_alerts: true })
    card()
    const toggle = await screen.findByRole('switch', { name: 'Subscription usage alerts' })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(toggle)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    await waitFor(() => expect(puts).toEqual([{ subscription_usage_alerts: false }]))
    const paths = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(paths).toContain(`/v1/settings/card/${ALERTS}`)
    expect(paths.some((p) => p.startsWith('/v1/chats'))).toBe(false)
    // Every call is the viewer's own sign-in (the cookie apiFetch sends): no
    // Authorization header, no session's token.
    for (const call of fetchSpy.mock.calls) {
      const headers = (call[1]?.headers ?? {}) as Record<string, string>
      expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain('authorization')
    }
  })

  it('shows a line while the server answers, the server sentence on a refusal, and another when it did not load', async () => {
    mockPrefs({ subscription_usage_alerts: true })
    let release: () => void = () => {}
    const held = new Promise<void>((r) => { release = r })
    const real = fetchSpy.getMockImplementation()!
    fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path.startsWith('/v1/settings/card/')) await held
      return real(path, init)
    })
    let view = card()
    await screen.findByText('Reading the setting.')
    expect(screen.queryByRole('switch')).toBeNull()
    await act(async () => { release() })
    await screen.findByRole('switch', { name: 'Subscription usage alerts' })
    view.unmount()
    // The sentence is the server's, whatever it says.
    mockPrefs({ subscription_usage_alerts: true })
    h.answer.current = { status: 403, detail: 'Your operator keeps this one.' }
    view = card()
    await screen.findByText('Your operator keeps this one.')
    expect(screen.queryByRole('switch')).toBeNull()
    view.unmount()
    // A failure, or a refusal that carries no sentence, reads as not loaded.
    for (const answer of [{ status: 500 }, { status: 404, detail: 'Not Found' }]) {
      h.answer.current = answer
      view = card()
      await screen.findByText('The setting did not load. Reload the page to see the card.')
      expect(screen.queryByRole('switch')).toBeNull()
      view.unmount()
    }
    expect(fetchSpy).not.toHaveBeenCalledWith(expect.stringContaining('ui-prefs'), expect.objectContaining({ method: 'PUT' }))
  })

  it('names the page under the label, and the heading when it says more than the label', async () => {
    mockPrefs({ subscription_usage_alerts: true, wake_word_enabled: false })
    const { unmount } = card()
    await screen.findByText('User Settings, General, under Notifications.')
    unmount()
    renderWithDialogs(<SettingCard settingId="user.general.wake-word" label="Wake word" catalogVersion={1} />, { wrapper })
    await screen.findByText('User Settings, General. The microphone is only on while the dashboard is open.')
  })

  it('a text saves trimmed and never blank', async () => {
    mockPrefs({})
    renderWithDialogs(<SettingCard settingId="user.general.display-name" label="Display Name" catalogVersion={1} />, { wrapper })
    const name = await screen.findByRole('textbox', { name: 'Display Name' })
    expect(name).toHaveValue('Dev')
    const save = screen.getByRole('button', { name: 'Save' })
    fireEvent.change(name, { target: { value: '   ' } })
    expect(save).toBeDisabled()
    fireEvent.change(name, { target: { value: '  Dev Two  ' } })
    expect(save).toBeEnabled()
    await act(async () => { fireEvent.click(save) })
    await waitFor(() => expect(puts).toEqual([{ display_name: 'Dev Two' }]))
  })

  it('a row the catalog no longer has shows one sentence', async () => {
    mockPrefs({})
    card('user.general.gone-row')
    await screen.findByText('This setting is not in this version.')
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('a device pick saves at once in this browser', async () => {
    mockPrefs({})
    renderWithDialogs(<SettingCard settingId="user.general.theme" label="Theme" catalogVersion={1} />, { wrapper })
    const pick = await screen.findByRole('combobox', { name: 'Theme' })
    expect(screen.getByText('Kept in this browser.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    fireEvent.change(pick, { target: { value: 'dark' } })
    await waitFor(() => expect(h.theme.set).toHaveBeenCalledWith('dark'))
  })

  const adminRow = CATALOG.find((e) => e.reads === 'admin' && e.value && 'confirm' in e.value && e.value.confirm)
  it.skipIf(!adminRow)('an admin row shows the server refusal to a member and saves for an admin behind a confirm', async () => {
    const binding = adminRow!.value as { read: { field: string }; confirm: string }
    mockPrefs({ [binding.read.field]: true })
    const { unmount } = renderWithDialogs(<SettingCard settingId={adminRow!.id} label={adminRow!.label} catalogVersion={1} />, { wrapper })
    await screen.findByText('An administrator changes this setting.')
    expect(screen.queryByRole('switch')).toBeNull()
    unmount()
    h.user.current = { sub: 'u1', role: 'admin', display_name: 'Admin', agent_roles: {} }
    renderWithDialogs(<SettingCard settingId={adminRow!.id} label={adminRow!.label} catalogVersion={1} />, { wrapper })
    const toggle = await screen.findByRole('switch', { name: adminRow!.label })
    fireEvent.click(toggle)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(binding.confirm)
    expect(puts).toEqual([])
    await act(async () => { personClick(within(dialog).getByRole('button', { name: 'Save' })) })
    await waitFor(() => expect(puts).toEqual([{ [binding.read.field]: false }]))
  })

  const cloudRow = CATALOG.find((e) => e.value && 'cloudManaged' in e.value && e.value.cloudManaged)
  it.skipIf(!adminRow || !cloudRow)('a setting the operator manages shows one line and no control', async () => {
    h.user.current = { sub: 'u1', role: 'admin', display_name: 'Admin', agent_roles: {} }
    const field = (adminRow!.value as { read: { field: string } }).read.field
    const cloudField = (cloudRow!.value as { read: { field: string } }).read.field
    // Forced by the operator: the save would answer 200 and change nothing.
    mockPrefs({ [field]: true, forced_keys: [field] })
    let view = renderWithDialogs(<SettingCard settingId={adminRow!.id} label={adminRow!.label} catalogVersion={1} />, { wrapper })
    await screen.findByText('Your operator manages this setting.')
    expect(screen.queryByRole('switch')).toBeNull()
    view.unmount()
    // On the cloud the operator owns the marked rows and no other.
    mockPrefs({ [field]: true, [cloudField]: true, cloud: true, forced_keys: [] })
    view = renderWithDialogs(<SettingCard settingId={cloudRow!.id} label={cloudRow!.label} catalogVersion={1} />, { wrapper })
    await screen.findByText('Your operator manages this setting.')
    view.unmount()
    renderWithDialogs(<SettingCard settingId={adminRow!.id} label={adminRow!.label} catalogVersion={1} />, { wrapper })
    await screen.findByRole('switch', { name: adminRow!.label })
  })
})
