import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactElement } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({ apiFetch: vi.fn(), toast: vi.fn() }))
vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: { sub: 'u1', role: 'user' } }) }))
vi.mock('@/lib/appToasts', () => ({ emitAppToast: h.toast, onAppToast: () => () => {} }))

import ChatTargetBanner from '@/components/chat/ChatTargetBanner'
import type { TargetMismatch } from '@/store/chatStore'
import type { ChatTargets } from '@/api/remoteMachines'
import { personClick } from './dialogsHarness'

// ─── ChatTargetBanner — pin-vs-current target mismatch notice ───────────────
// Dismissal is sticky per (chat, resolved target): the same mismatch never
// re-shows, a NEW resolved target shows once again, and dismissal only hides
// the banner (the kebab row is the permanent home — its own test file). Its
// button opens the chat's places (the "Runs on" chip's menu) and moves over
// HTTP; a place a person chose hides it.

const MISMATCH: TargetMismatch = {
  pinnedTarget: 'local',
  pinnedLabel: 'the OtoDock platform',
  resolvedTarget: 'm-attic',
  resolvedLabel: 'Attic PC',
}

const TARGETS: ChatTargets = {
  current: { target: 'local', label: 'OtoDock platform', pairing: '', state: '', owner: '' },
  default_target: 'm-attic',
  targets: [
    { target: 'local', label: 'OtoDock platform', pairing: '', state: '', reachable: true,
      is_default: false, is_current: true, pickable: true, why_not: '', allowed: true },
    { target: 'm-attic', label: 'Attic PC', pairing: 'user', state: 'online', reachable: true,
      is_default: true, is_current: false, pickable: true, why_not: '', allowed: true },
  ],
  can_move: true, refusal: '', lock: null, moving: null, place_chosen: false,
}

const answer = (status: number, body: unknown) => new Response(JSON.stringify(body), { status })
let targets: ChatTargets

beforeEach(() => {
  localStorage.clear()
  targets = TARGETS
  h.apiFetch.mockReset()
  h.apiFetch.mockImplementation(async (path: string) => {
    if (path === '/v1/chats/c1/targets') return answer(200, targets)
    if (path === '/v1/chats/c1/move') return answer(202, { move_id: 'mv-1', to_label: 'Attic PC', memory: 'files' })
    return answer(404, { detail: 'Not found' })
  })
})

const qc = () => new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

function renderBanner(over: Partial<Parameters<typeof ChatTargetBanner>[0]> = {}) {
  const client = qc()
  const banner = (props: Partial<Parameters<typeof ChatTargetBanner>[0]>): ReactElement => (
    <QueryClientProvider client={client}>
      <ChatTargetBanner chatId="c1" mismatch={MISMATCH} moveDisabled={false} onMove={() => {}} {...over} {...props} />
    </QueryClientProvider>
  )
  const view = render(banner({}))
  return { ...view, rerenderBanner: (props: Partial<Parameters<typeof ChatTargetBanner>[0]>) => view.rerender(banner(props)) }
}

/** The banner's button once the chat's places have loaded. */
async function moveButton() {
  const button = () => screen.getByText('Move this chat…').closest('button') as HTMLButtonElement
  await waitFor(() => expect(button()).toHaveAttribute('aria-haspopup', 'menu'))
  return button()
}

const moveCalls = () => h.apiFetch.mock.calls.filter(([p]) => p === '/v1/chats/c1/move')

describe('ChatTargetBanner', () => {
  it('local-pinned mismatch shows the "still runs on the OtoDock platform" copy', () => {
    renderBanner()
    expect(
      screen.getByText('This chat still runs on the OtoDock platform. New chats run on Attic PC.'),
    ).toBeTruthy()
    expect(screen.getByText('Move this chat…')).toBeTruthy()
  })

  it('machine-pinned mismatch names the pinned machine', () => {
    renderBanner({ mismatch: { ...MISMATCH, pinnedTarget: 'm-old', pinnedLabel: 'Old box' } })
    expect(
      screen.getByText('This chat runs on Old box. New chats run on Attic PC.'),
    ).toBeTruthy()
  })

  it('renders nothing when there is no mismatch', () => {
    const { container } = renderBanner({ mismatch: null })
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing while the place was chosen by a person', async () => {
    targets = { ...TARGETS, place_chosen: true }
    renderBanner()
    await waitFor(() => expect(screen.queryByText(/new chats run on/i)).toBeNull())
  })

  it('X records the resolved target under oto.chattarget.dismissed.<chatId> and hides', () => {
    renderBanner()
    fireEvent.click(screen.getByTitle('Dismiss'))
    expect(screen.queryByText(/new chats run on/i)).toBeNull()
    expect(JSON.parse(localStorage.getItem('oto.chattarget.dismissed.c1')!)).toEqual(['m-attic'])
  })

  it('dismissal is sticky per resolved target — a NEW target shows once again', () => {
    const { rerenderBanner } = renderBanner()
    fireEvent.click(screen.getByTitle('Dismiss'))
    // Same resolved target on a re-render stays hidden.
    rerenderBanner({ mismatch: { ...MISMATCH } })
    expect(screen.queryByText(/new chats run on/i)).toBeNull()
    // The agent moved again — a different resolved target re-shows.
    rerenderBanner({ mismatch: { ...MISMATCH, resolvedTarget: 'm-basement', resolvedLabel: 'Basement PC' } })
    expect(screen.getByText(/new chats run on Basement PC/i)).toBeTruthy()
  })

  it('a fresh mount stays hidden for a previously dismissed target', () => {
    renderBanner()
    fireEvent.click(screen.getByTitle('Dismiss'))
    renderBanner()
    expect(screen.queryByText(/new chats run on/i)).toBeNull()
  })

  it('the move button is disabled while the chat streams/warms', async () => {
    renderBanner({ moveDisabled: true })
    const btn = await moveButton()
    expect(btn.disabled).toBe(true)
  })

  it("a chat that cannot move keeps the button disabled with the server's sentence", async () => {
    targets = { ...TARGETS, lock: { sentence: "This chat is running on Ann's computer." } }
    renderBanner()
    await waitFor(() => expect(screen.getByText('Move this chat…').closest('button'))
      .toHaveAttribute('title', "This chat is running on Ann's computer."))
    expect(screen.getByText('Move this chat…').closest('button')).toBeDisabled()
  })

  it('the button opens the places; a pick asks, then moves over HTTP (never onMove)', async () => {
    const onMove = vi.fn()
    renderBanner({ onMove })
    fireEvent.click(await moveButton())
    fireEvent.click(screen.getByRole('menuitem', { name: /Attic PC/ }))
    expect(screen.getByText('Move this chat to Attic PC?')).toBeTruthy()
    personClick(screen.getByText('Move chat'))
    expect(screen.queryByText('Move this chat to Attic PC?')).toBeNull()
    await waitFor(() => expect(moveCalls()).toHaveLength(1))
    expect(JSON.parse((moveCalls()[0][1] as RequestInit).body as string)).toEqual({ target: 'm-attic', from: 'local' })
    expect(onMove).not.toHaveBeenCalled()
  })

  it('a turn starting closes the confirm for good: it does not come back when the turn ends', async () => {
    const { rerenderBanner } = renderBanner()
    fireEvent.click(await moveButton())
    fireEvent.click(screen.getByRole('menuitem', { name: /Attic PC/ }))
    expect(screen.getByText('Move this chat to Attic PC?')).toBeTruthy()
    rerenderBanner({ moveDisabled: true })
    expect(screen.queryByText('Move this chat to Attic PC?')).toBeNull()
    rerenderBanner({ moveDisabled: false })
    expect(screen.queryByText('Move this chat to Attic PC?')).toBeNull()
    expect(moveCalls()).toHaveLength(0)
  })

  it('Cancel closes the confirm without moving', async () => {
    renderBanner()
    fireEvent.click(await moveButton())
    fireEvent.click(screen.getByRole('menuitem', { name: /Attic PC/ }))
    personClick(screen.getByText('Cancel'))
    expect(moveCalls()).toHaveLength(0)
    expect(screen.queryByText('Move this chat to Attic PC?')).toBeNull()
  })
})
