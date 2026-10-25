// Where a chat runs, chosen per chat: the "Runs on" chip (a new chat's
// start place, the chat's place and its menu, the confirm's words by memory,
// the move posted from the place the chip showed, the moving state, a
// refusal's toast, the no-menu cases), the chip's menu per role, the lock
// line above the composer, the moved card in the history, the page's answer
// to a `chat_moved` frame (the chip's query read again, a failed move said,
// the chat resumed), and the task dialog (the places of the task's run
// person, the save of its extra field).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState, type ReactElement, type ReactNode } from 'react'
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({ apiFetch: vi.fn(), toast: vi.fn() }))
vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: { sub: 'u1', role: 'user' } }) }))
vi.mock('@/lib/appToasts', () => ({ emitAppToast: h.toast, onAppToast: () => () => {} }))

import RunsOnChip, { ChatLockNotice, MoveRing } from '@/components/chat/RunsOnChip'
import SystemEvent from '@/components/chat/SystemEvent'
import BlockRenderer from '@/components/chat/ChatBlockRenderer'
import { CHAT_MOVED_REASON, OUT, SYSTEM_SUBTYPE, WAKE_SOURCE, WIRE } from '@/api/wireEvents'
import {
  chatTargetsKey, leavesOffline, MOVE_MEMORY, moveMemoryOf, type AgentRunTargets, type ChatTargets, type RunTarget,
} from '@/api/remoteMachines'
import type { Task } from '@/api/tasks'
import { TaskSettingsDialog, TaskSettingsModal } from '@/pages/agent/AgentSchedules.modals'
import { useAgentChatStream } from '@/pages/agent/chat/useAgentChatStream'
import { personClick } from './dialogsHarness'
import { FakeWebSocket } from './fakeWebSocket'

const SERVER = {
  target: 'local', label: 'OtoDock platform', pairing: '' as const, state: '' as const, reachable: true,
  is_default: true, pickable: true, why_not: '', allowed: true,
}
const ATTIC = {
  target: 'm-attic', label: 'Attic PC', pairing: 'user' as const, state: 'online' as const, reachable: true,
  is_default: false, pickable: true, why_not: '', allowed: true,
}
const DRILL = {
  target: 'm-drill', label: 'drill-sat', pairing: 'admin' as const, state: 'disconnected' as const, reachable: false,
  is_default: false, pickable: false, why_not: 'drill-sat is offline. Try again when it is back.', allowed: true,
}

const ON_SERVER: ChatTargets = {
  current: { target: 'local', label: 'OtoDock platform', pairing: '', state: '', owner: '' },
  default_target: 'local',
  targets: [{ ...SERVER, is_current: true }, { ...ATTIC, is_current: false }, { ...DRILL, is_current: false }],
  can_move: true, refusal: '', lock: null, moving: null, place_chosen: false,
}

// The chat sits on drill-sat, which went offline: a move carries a summary.
const ON_DRILL: ChatTargets = {
  ...ON_SERVER,
  current: { target: 'm-drill', label: 'drill-sat', pairing: 'admin', state: 'disconnected', owner: '' },
  targets: [
    { ...SERVER, is_current: false },
    { ...ATTIC, is_current: false },
    { ...DRILL, is_current: true, pickable: true, why_not: '' },
  ],
}

const RUN_TARGETS: AgentRunTargets = {
  default_target: 'm-attic',
  targets: [{ ...SERVER, is_default: false }, { ...ATTIC, is_default: true }],
}

const answer = (status: number, body: unknown) => new Response(JSON.stringify(body), { status })

let targets: ChatTargets
let moveAnswer: () => Response

beforeEach(() => {
  targets = ON_SERVER
  moveAnswer = () => answer(202, { move_id: 'mv-1', to_label: 'Attic PC', memory: 'files' })
  h.toast.mockReset()
  h.apiFetch.mockReset()
  h.apiFetch.mockImplementation(async (path: string) => {
    if (path === '/v1/chats/c1/targets') return answer(200, targets)
    if (path === '/v1/chats/c1/move') return moveAnswer()
    if (path.startsWith('/v1/agents/dev/run-targets')) return answer(200, RUN_TARGETS)
    if (path.startsWith('/v1/tasks/') && path.endsWith('/edit')) return answer(200, { ok: true })
    return answer(404, { detail: 'Not found' })
  })
})

function withQuery(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return { qc, ...render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>) }
}

const chip = (over: Partial<Parameters<typeof RunsOnChip>[0]> = {}) => (
  <RunsOnChip chatId="c1" agentName="dev" disabledReason="" {...over} />
)

const moveCalls = () => h.apiFetch.mock.calls.filter(([p]) => p === '/v1/chats/c1/move')

describe('the "Runs on" chip', () => {
  it("is an icon pill of the mode pill's shape, the place in its tooltip", async () => {
    withQuery(chip())
    const pill = await screen.findByTestId('runs-on-chip')
    expect(pill).toHaveTextContent('')
    expect(pill.querySelector('svg')).not.toBeNull()
    expect(pill.className).toContain('w-7 h-7 shrink-0 rounded-control border')
    expect(pill).toHaveAttribute('title', 'Runs on OtoDock platform')
    expect(pill).toHaveAttribute('aria-label', 'Runs on OtoDock platform')
  })

  it('a new chat picks where it starts from the same menu, with no confirm', async () => {
    const onPickPlace = vi.fn()
    withQuery(chip({ chatId: null, engine: 'codex-cli', onPickPlace }))
    const pill = await screen.findByTestId('runs-on-chip')
    expect(pill).toHaveAttribute('title', 'Starts on Attic PC')
    expect(h.apiFetch.mock.calls.some(([p]) => p === '/v1/agents/dev/run-targets?engine=codex-cli')).toBe(true)
    fireEvent.click(pill)
    const menu = screen.getByRole('menu', { name: 'Run this chat on' })
    const rows = within(menu).getAllByRole('menuitem')
    expect(rows[1]).toHaveTextContent('Attic PC, default, current')
    fireEvent.click(rows[0])
    expect(onPickPlace).toHaveBeenCalledWith('local', false)
    expect(screen.queryByText(/Move this chat/)).toBeNull()
    expect(moveCalls()).toHaveLength(0)
  })

  it('a pick the menu no longer offers is dropped for the default', async () => {
    const onPickPlace = vi.fn()
    withQuery(chip({ chatId: null, pickedPlace: 'm-gone', onPickPlace }))
    await screen.findByTestId('runs-on-chip')
    await waitFor(() => expect(onPickPlace).toHaveBeenCalledWith('m-attic', true))
  })

  it('a new chat shows the place picked so far', async () => {
    withQuery(chip({ chatId: null, pickedPlace: 'local', onPickPlace: () => {} }))
    expect(await screen.findByTestId('runs-on-chip')).toHaveAttribute('title', 'Starts on OtoDock platform')
  })

  it("shows the chat's place and lists the places with the current one checked", async () => {
    withQuery(chip())
    const pill = await screen.findByTestId('runs-on-chip')
    expect(pill).toHaveAttribute('title', 'Runs on OtoDock platform')
    expect(pill.tagName).toBe('BUTTON')
    fireEvent.click(pill)
    const menu = screen.getByRole('menu', { name: 'Run this chat on' })
    const rows = within(menu).getAllByRole('menuitem')
    expect(rows).toHaveLength(3)
    // The current place: checked, the default marked, nothing to pick.
    expect(rows[0]).toHaveTextContent('OtoDock platform, default, current')
    expect(rows[0]).toBeDisabled()
    expect(rows[1]).toHaveTextContent('Attic PC')
    expect(rows[1]).toBeEnabled()
    // Offline: the red dot says it, the sentence stays the row's tooltip,
    // the row disabled.
    expect(within(rows[2]).queryByTestId('place-reason')).toBeNull()
    expect(rows[2]).toHaveAttribute('title', 'drill-sat is offline. Try again when it is back.')
    expect(rows[2]).toBeDisabled()
  })

  it('a pick asks first, then posts the move from the place the chip showed and reads moving', async () => {
    const onMoved = vi.fn()
    withQuery(chip({ onMoved }))
    fireEvent.click(await screen.findByTestId('runs-on-chip'))
    fireEvent.click(screen.getByRole('menuitem', { name: /Attic PC/ }))
    expect(screen.getByText('Move this chat to Attic PC?')).toBeInTheDocument()
    expect(screen.getByText('The chat continues on Attic PC.')).toBeInTheDocument()
    expect(moveCalls()).toHaveLength(0)
    personClick(screen.getByText('Move chat'))
    await waitFor(() => expect(moveCalls()).toHaveLength(1))
    const [, init] = moveCalls()[0] as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ target: 'm-attic', from: 'local' })
    const pill = await screen.findByTestId('runs-on-chip')
    await waitFor(() => expect(pill).toHaveAttribute('title', 'Moving to Attic PC…'))
    expect(pill).toHaveAttribute('aria-disabled', 'true')
    expect(onMoved).toHaveBeenCalledTimes(1)
  })

  it("Cancel moves nothing", async () => {
    withQuery(chip())
    fireEvent.click(await screen.findByTestId('runs-on-chip'))
    fireEvent.click(screen.getByRole('menuitem', { name: /Attic PC/ }))
    personClick(screen.getByText('Cancel'))
    expect(screen.queryByText('Move this chat to Attic PC?')).toBeNull()
    expect(moveCalls()).toHaveLength(0)
  })

  it('a move from a machine that is offline carries a summary, and the confirm says so', async () => {
    targets = ON_DRILL
    withQuery(chip())
    const pill = await screen.findByTestId('runs-on-chip')
    expect(pill).toHaveAttribute('title', 'Runs on drill-sat')
    fireEvent.click(pill)
    fireEvent.click(screen.getByRole('menuitem', { name: /OtoDock platform/ }))
    expect(screen.getByText('Move this chat to the OtoDock platform?')).toBeInTheDocument()
    expect(screen.getByText(
      'drill-sat is offline, so the chat continues on the OtoDock platform from a summary of the conversation.',
    )).toBeInTheDocument()
    personClick(screen.getByText('Move chat'))
    await waitFor(() => expect(moveCalls()).toHaveLength(1))
    expect(JSON.parse((moveCalls()[0][1] as RequestInit).body as string)).toEqual({ target: 'local', from: 'm-drill' })
  })

  it('a move with a place whose satellite cannot move the files says it continues from a summary', async () => {
    targets = {
      ...ON_SERVER,
      targets: [{ ...SERVER, is_current: true, moves_files: true },
        { ...ATTIC, is_current: false, moves_files: false }, { ...DRILL, is_current: false }],
    }
    withQuery(chip())
    fireEvent.click(await screen.findByTestId('runs-on-chip'))
    fireEvent.click(screen.getByRole('menuitem', { name: /Attic PC/ }))
    expect(screen.getByText(
      'The chat continues on Attic PC from a summary of the conversation.',
    )).toBeInTheDocument()
    expect(screen.queryByText(/is offline/)).toBeNull()
  })

  it("the memory a move carries reads both places, an older proxy's answer as files", () => {
    const onAttic: ChatTargets = {
      ...ON_SERVER,
      current: { target: 'm-attic', label: 'Attic PC', pairing: 'user', state: 'online', owner: '' },
      targets: [{ ...SERVER, is_current: false }, { ...ATTIC, is_current: true, moves_files: false }],
    }
    // The place the chat leaves cannot move the files, wherever it goes.
    expect(moveMemoryOf(onAttic, 'local')).toBe(MOVE_MEMORY.SUMMARY)
    expect(leavesOffline(onAttic)).toBe(false)
    // No field at all (an older proxy): the files, as before.
    expect(moveMemoryOf(ON_SERVER, 'm-attic')).toBe(MOVE_MEMORY.FILES)
    // An offline place says so, whatever its satellite.
    expect(moveMemoryOf(ON_DRILL, 'local')).toBe(MOVE_MEMORY.SUMMARY)
    expect(leavesOffline(ON_DRILL)).toBe(true)
  })

  it("a refusal shows the server's sentence and reads the place again", async () => {
    moveAnswer = () => answer(409, { detail: 'This chat just moved to drill-sat.' })
    withQuery(chip())
    fireEvent.click(await screen.findByTestId('runs-on-chip'))
    fireEvent.click(screen.getByRole('menuitem', { name: /Attic PC/ }))
    const reads = h.apiFetch.mock.calls.filter(([p]) => p === '/v1/chats/c1/targets').length
    personClick(screen.getByText('Move chat'))
    await waitFor(() => expect(h.toast).toHaveBeenCalledWith(expect.objectContaining({
      title: 'The chat was not moved', body: 'This chat just moved to drill-sat.',
    })))
    await waitFor(() => expect(h.apiFetch.mock.calls.filter(([p]) => p === '/v1/chats/c1/targets').length)
      .toBeGreaterThan(reads))
    expect(screen.getByTestId('runs-on-chip')).toHaveAttribute('title', 'Runs on OtoDock platform')
  })

  it('mid-turn the pill opens no menu, and a tap shows the place and why', async () => {
    withQuery(chip({ disabledReason: 'Finish or stop the current turn before moving the chat.' }))
    const pill = await screen.findByTestId('runs-on-chip')
    expect(pill).toHaveAttribute('aria-disabled', 'true')
    expect(pill).not.toHaveAttribute('aria-haspopup')
    expect(pill).toHaveAttribute('title',
      'Runs on OtoDock platform. Finish or stop the current turn before moving the chat.')
    fireEvent.click(pill)
    expect(screen.queryByRole('menu')).toBeNull()
    const note = screen.getByRole('note')
    expect(note).toHaveTextContent('Runs on OtoDock platform')
    expect(note).toHaveTextContent('Finish or stop the current turn before moving the chat.')
  })

  it('a move the server reports running reads moving and takes no click', async () => {
    targets = { ...ON_SERVER, moving: { to_label: 'drill-sat' } }
    withQuery(chip())
    const pill = await screen.findByTestId('runs-on-chip')
    expect(pill).toHaveAttribute('title', 'Moving to drill-sat…')
    expect(screen.getByRole('button', { name: 'Moving to drill-sat…' })).toBe(pill)
    expect(pill).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(pill)
    expect(screen.queryByRole('menu')).toBeNull()
    expect(screen.getByRole('note')).toHaveTextContent('Moving to drill-sat…')
  })

  it("a move's copy draws a ring of its progress in place of the glyph", async () => {
    targets = { ...ON_SERVER, moving: { to_label: 'drill-sat', progress: { done: 25, total: 100, phase: 'copy' } } }
    withQuery(chip())
    const pill = await screen.findByTestId('runs-on-chip')
    await waitFor(() => expect(within(pill).getByTestId('move-ring')).toHaveAttribute('data-pct', '25'))
    expect(within(pill).queryByTestId('move-spinner')).toBeNull()
    expect(pill).toHaveAttribute('title', 'Moving to drill-sat…')
  })

  it('the staging spins, the commit fills the ring and spins inside it', () => {
    const { rerender } = render(<MoveRing progress={{ done: 0, total: 100, phase: 'stage' }} />)
    expect(screen.getByTestId('move-spinner')).toBeInTheDocument()
    expect(screen.queryByTestId('move-ring')).toBeNull()
    rerender(<MoveRing progress={{ done: 100, total: 100, phase: 'commit' }} />)
    expect(screen.getByTestId('move-ring')).toHaveAttribute('data-pct', '100')
    expect(screen.getByTestId('move-ring').querySelector('.animate-spin')).not.toBeNull()
  })

  it('a move of unknown size spins, and the glyph returns when it ends', async () => {
    targets = { ...ON_SERVER, moving: { to_label: 'drill-sat', progress: { done: 0, total: 0, phase: 'stage' } } }
    const { qc } = withQuery(chip())
    const pill = await screen.findByTestId('runs-on-chip')
    await waitFor(() => expect(within(pill).getByTestId('move-spinner')).toBeInTheDocument())
    expect(within(pill).queryByTestId('move-ring')).toBeNull()
    act(() => { qc.setQueryData(chatTargetsKey('c1'), { ...ON_SERVER, moving: null }) })
    await waitFor(() => expect(within(pill).queryByTestId('move-spinner')).toBeNull())
    expect(pill.querySelector('svg')).not.toBeNull()
    expect(pill).toHaveAttribute('title', 'Runs on OtoDock platform')
  })

  it('a chat that cannot move shows its place with the refusal as the tooltip, no menu', async () => {
    targets = { ...ON_SERVER, can_move: false, refusal: 'Direct LLM chats run on the OtoDock platform.' }
    withQuery(chip())
    const pill = await screen.findByTestId('runs-on-chip')
    expect(pill).not.toHaveAttribute('aria-haspopup')
    expect(pill).toHaveAttribute('title',
      'Runs on OtoDock platform. Direct LLM chats run on the OtoDock platform.')
    // A touch screen has no tooltip: a tap shows the same words.
    fireEvent.click(pill)
    expect(screen.queryByRole('menu')).toBeNull()
    expect(screen.getByRole('note')).toHaveTextContent('Direct LLM chats run on the OtoDock platform.')
  })

  it("a chat on someone's computer shows the lock sentence as the tooltip, no menu", async () => {
    targets = {
      ...ON_SERVER,
      current: { target: 'm-ann', label: "Ann's laptop", pairing: 'user', state: 'online', owner: 'Ann' },
      lock: { sentence: "This chat is running on Ann's computer." },
    }
    withQuery(chip())
    const pill = await screen.findByTestId('runs-on-chip')
    expect(pill).not.toHaveAttribute('aria-haspopup')
    expect(pill).toHaveAttribute('title', "Runs on Ann's laptop. This chat is running on Ann's computer.")
    fireEvent.click(pill)
    const note = screen.getByRole('note')
    expect(note).toHaveTextContent("Runs on Ann's laptop")
    expect(note).toHaveTextContent("This chat is running on Ann's computer.")
  })
})

// The places of the role table as the server's `targets_view` lists them.
const SERVER_NOT_DEFAULT: RunTarget = { ...SERVER, is_default: false }
const DEN: RunTarget = {
  target: 'm-den', label: 'Den laptop', pairing: 'user', state: 'disconnected', reachable: false,
  is_default: false, pickable: false, why_not: 'Den laptop is offline.', allowed: true,
}
const BUILD: RunTarget = {
  target: 'm-build', label: 'build-box', pairing: 'admin', state: 'online', reachable: true,
  is_default: false, pickable: true, why_not: '', allowed: true,
}
const DRILL_OFFLINE: RunTarget = { ...DRILL, why_not: 'drill-sat is offline.' }
const ANN_DESKTOP: RunTarget = {
  target: 'm-ann-desk', label: "Ann's desktop", pairing: 'user', state: 'online', reachable: true,
  is_default: false, pickable: true, why_not: '', allowed: true,
}
const ANN_LAPTOP: RunTarget = { ...ANN_DESKTOP, target: 'm-ann', label: "Ann's laptop" }

const NOT_MOVER = 'Only someone who can continue this chat can move it.'
const KIND_STAYS = 'This kind of chat stays where it runs.'
const VOICE_HOLDS = 'A voice conversation is using this chat. End it before moving the chat.'

/** A chat on the server as `targets_view` answers it: `places` in the
 *  server's order (the server, the person's own machines, then the admin
 *  ones, each by name), the default the one marked. */
function onServer(places: RunTarget[], over: Partial<ChatTargets> = {}): ChatTargets {
  return {
    current: { target: 'local', label: 'OtoDock platform', pairing: '', state: '', owner: '' },
    default_target: places.find((p) => p.is_default)?.target ?? 'local',
    targets: places.map((p) => ({ ...p, is_current: p.target === 'local' })),
    can_move: true, refusal: '', lock: null, moving: null, place_chosen: false,
    ...over,
  }
}

/** What the menu shows of one place. */
interface Row { label: string; default?: true; checked?: true; disabled?: true; title?: string }

function rowView(row: HTMLElement): Row {
  const view: Row = { label: row.querySelector('[data-place-label]')?.textContent ?? '' }
  if (row.textContent?.includes(', default')) view.default = true
  if (within(row).queryByTestId('place-tick')) view.checked = true
  if ((row as HTMLButtonElement).disabled) view.disabled = true
  const title = row.getAttribute('title')
  if (title) view.title = title
  return view
}

/** The chat's own place: checked, nothing to pick. */
const HERE: Row = { label: 'OtoDock platform', checked: true, disabled: true }

const ROLE_CASES: { name: string; answer: ChatTargets; menu?: Row[]; refusal?: string }[] = [
  {
    name: 'a viewer, user or editor on a normal agent: the server, their default and their own machines',
    answer: onServer([SERVER_NOT_DEFAULT, { ...ATTIC, is_default: true }, DEN]),
    menu: [HERE, { label: 'Attic PC', default: true }, { label: 'Den laptop', disabled: true, title: 'Den laptop is offline.' }],
  },
  {
    name: 'an editor whose agent default is an admin machine: the server as their default, the machine not listed',
    answer: onServer([SERVER, ATTIC]),
    menu: [{ ...HERE, default: true }, { label: 'Attic PC' }],
  },
  {
    name: 'a manager: the server and the admin default only',
    answer: onServer([SERVER_NOT_DEFAULT, { ...BUILD, is_default: true }]),
    menu: [HERE, { label: 'build-box', default: true }],
  },
  {
    name: 'a platform admin: the server and every admin machine, an offline one disabled',
    answer: onServer([SERVER_NOT_DEFAULT, { ...BUILD, is_default: true }, DRILL_OFFLINE]),
    menu: [HERE, { label: 'build-box', default: true }, { label: 'drill-sat', disabled: true, title: 'drill-sat is offline.' }],
  },
  {
    name: "a platform admin on a colleague's chat: the owner's default and the owner's machines",
    answer: onServer([SERVER_NOT_DEFAULT, ANN_DESKTOP, { ...ANN_LAPTOP, is_default: true }]),
    menu: [HERE, { label: "Ann's desktop" }, { label: "Ann's laptop", default: true }],
  },
  {
    name: 'a Shared-only viewer: no menu',
    answer: onServer([SERVER, ATTIC], { can_move: false, refusal: NOT_MOVER }),
    refusal: NOT_MOVER,
  },
  {
    name: 'a Shared-only contributor: no menu',
    answer: onServer([SERVER, ATTIC], { can_move: false, refusal: NOT_MOVER }),
    refusal: NOT_MOVER,
  },
  {
    name: 'a phone chat: no menu',
    answer: onServer([SERVER, ATTIC], { can_move: false, refusal: KIND_STAYS }),
    refusal: KIND_STAYS,
  },
  {
    name: 'a voice conversation attached: no menu',
    answer: onServer([SERVER, ATTIC], { can_move: false, refusal: VOICE_HOLDS }),
    refusal: VOICE_HOLDS,
  },
  {
    name: 'personal computers turned off on the platform: the own machine absent',
    answer: onServer([SERVER]),
    menu: [{ ...HERE, default: true }],
  },
]

describe("the chip's menu per role", () => {
  it.each(ROLE_CASES)('$name', async ({ answer, menu, refusal }) => {
    targets = answer
    withQuery(chip())
    const pill = await screen.findByTestId('runs-on-chip')
    fireEvent.click(pill)
    const shown = screen.queryByRole('menu', { name: 'Run this chat on' })
    expect({
      tooltip: pill.getAttribute('title'),
      menu: shown && within(shown).getAllByRole('menuitem').map(rowView),
    }).toEqual({
      tooltip: refusal ? `Runs on OtoDock platform. ${refusal}` : 'Runs on OtoDock platform',
      menu: menu ?? null,
    })
  })
})

/** The places menu of the chip, opened by a click. */
async function openMenu(answer: ChatTargets) {
  targets = answer
  withQuery(chip())
  fireEvent.click(await screen.findByTestId('runs-on-chip'))
  return screen.getByRole('menu', { name: 'Run this chat on' })
}

const LONG: RunTarget = {
  target: 'm-long', label: 'Homelab Trusted Services VM on the second rack', pairing: 'admin', state: 'online',
  reachable: true, is_default: false, pickable: true, why_not: '', allowed: true,
}

describe('the menu rows', () => {
  it('a click opens with no row painted by its focus, and the arrows still walk the rows', async () => {
    const menu = await openMenu(onServer([SERVER, ATTIC, BUILD]))
    const rows = within(menu).getAllByRole('menuitem')
    for (const row of rows) {
      // jsdom computes no :focus-visible: the class is the proof here, the
      // walk shows the paint in a browser.
      expect(row.className).not.toMatch(/(^|\s)focus:bg-/)
      expect(row.className).toContain('focus-visible:bg-surface-hover')
    }
    // The first row a person can pick still takes the focus.
    await waitFor(() => expect(document.activeElement).toBe(rows[1]))
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(rows[2])
  })

  it("each place's dot is the badge's colour: the platform green, offline red whoever paired it", async () => {
    const menu = await openMenu(onServer([
      SERVER, ATTIC, DEN, DRILL_OFFLINE,
      { ...BUILD, target: 'm-paused', label: 'paused-box', state: 'paused', reachable: false, pickable: false,
        why_not: 'paused-box is offline.' },
      { ...BUILD, target: 'm-slow', label: 'slow-box', state: 'stale' },
      { ...BUILD, target: 'm-new', label: 'new-box', state: 'never_connected', reachable: false, pickable: false,
        why_not: 'new-box is offline.' },
    ]))
    const dots = within(menu).getAllByTestId('place-dot')
      .map((d) => /bg-status-[a-z]+-solid/.exec(d.className)?.[0])
    expect(dots).toEqual(['bg-status-success-solid', 'bg-status-success-solid', 'bg-status-danger-solid',
      'bg-status-danger-solid', 'bg-status-warning-solid', 'bg-status-warning-solid', 'bg-status-info-solid'])
    const rows = within(menu).getAllByRole('menuitem')
    // The second line says only what the dot does not: no "offline" sentence.
    expect(rows.map((r) => within(r).queryByTestId('place-reason')?.textContent ?? ''))
      .toEqual(['', '', '', '', 'paused', 'slow connection', ''])
    // A screen reader still hears the state the dot shows.
    expect(rows[2]).toHaveTextContent('Den laptop, offline')
    expect(rows[1]).toHaveTextContent('Attic PC, online')
  })

  it('keeps the reasons the dot cannot say: the engine, and the guard even while offline', async () => {
    const menu = await openMenu(onServer([
      SERVER,
      { ...ATTIC, pickable: false, why_not: "Attic PC can't run this chat's engine." },
      { ...DEN, allowed: false, why_not: "You can't run this chat on Den laptop." },
    ]))
    const rows = within(menu).getAllByRole('menuitem')
    expect(within(rows[1]).getByTestId('place-reason')).toHaveTextContent("Attic PC can't run this chat's engine.")
    expect(within(rows[2]).getByTestId('place-reason')).toHaveTextContent("You can't run this chat on Den laptop.")
    expect(rows[1]).toBeDisabled()
    expect(rows[2]).toBeDisabled()
  })

  it('the tick sits right of "default" on the current default place', async () => {
    const menu = await openMenu(onServer([SERVER, ATTIC]))
    const row = within(menu).getAllByRole('menuitem')[0]
    const tick = within(row).getByTestId('place-tick')
    // Glued to the last character: it never starts a line alone.
    expect(tick.parentElement).toHaveClass('whitespace-nowrap')
    expect(tick.parentElement?.textContent).toBe('t')
    expect(row.querySelector('[data-place-label]')?.textContent).toBe('OtoDock platform')
    expect(within(menu).getAllByTestId('place-tick')).toHaveLength(1)
  })

  it('a long name wraps, its tick right of its last word', async () => {
    const menu = await openMenu({
      ...onServer([SERVER_NOT_DEFAULT, LONG]),
      current: { target: 'm-long', label: LONG.label, pairing: 'admin', state: 'online', owner: '' },
      targets: [{ ...SERVER_NOT_DEFAULT, is_current: false }, { ...LONG, is_current: true }],
    })
    const row = within(menu).getAllByRole('menuitem')[1]
    const title = row.querySelector('[data-place-label]')
    expect(title?.textContent).toBe(LONG.label)
    expect(title?.parentElement).toHaveClass('break-words')
    expect(title?.parentElement).not.toHaveClass('truncate')
    const tick = within(row).getByTestId('place-tick')
    expect(tick.parentElement).toHaveClass('whitespace-nowrap')
    expect(tick.parentElement?.textContent).toBe('k')
    expect(row).toHaveTextContent(`${LONG.label}, current, online`)
  })
})

describe('the lock line', () => {
  it('shows the sentence, and nothing without one', () => {
    const { rerender } = render(<ChatLockNotice sentence="This chat is running on Ann's computer." />)
    expect(screen.getByTestId('chat-lock-notice')).toHaveTextContent("This chat is running on Ann's computer.")
    rerender(<ChatLockNotice sentence="" />)
    expect(screen.queryByTestId('chat-lock-notice')).toBeNull()
  })
})

describe('the moved card', () => {
  it('names the move, or says it did not finish', () => {
    const { rerender } = render(<SystemEvent subtype={SYSTEM_SUBTYPE.CHAT_MOVED}
      message="This chat moved from the OtoDock platform to Attic PC. Its memory came with it." />)
    expect(screen.getByTestId('chat-moved')).toHaveTextContent('Chat moved')
    expect(screen.getByText(/Its memory came with it/)).toBeInTheDocument()
    rerender(<SystemEvent subtype={SYSTEM_SUBTYPE.CHAT_MOVED} reason={CHAT_MOVED_REASON.FAILED}
      message="The move to Attic PC did not finish: Attic PC went offline. The chat stays on the OtoDock platform." />)
    expect(screen.getByTestId('chat-moved')).toHaveTextContent('Chat move did not finish')
    expect(screen.getByText(/The chat stays on the OtoDock platform/)).toBeInTheDocument()
  })
})

describe('the tools card', () => {
  it('names the tools still installing on the machine', () => {
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.TOOLS_INSTALLING}
      message="Two tools are still installing on my laptop: Blender and Schedules. Start a new chat on this machine once they are done to use them." />)
    expect(screen.getByTestId('tools-installing')).toHaveTextContent('Tools on this computer')
    expect(screen.getByText(/Blender and Schedules/)).toBeInTheDocument()
  })
})

describe("the page's answer to chat_moved", () => {
  beforeEach(() => {
    FakeWebSocket.instances.length = 0
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
  })
  afterEach(() => vi.unstubAllGlobals())

  function mountStream(urlChatId = 'c1') {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const resetSession = vi.fn()
    const hook = renderHook(() => useAgentChatStream({
      agents: [], urlChatId, agentDefaultModel: '', agentExecutionPath: '', agentName: 'dev',
      navigate: vi.fn(), isFavoriteAgent: false, preWarmPath: '', warmingUp: false, setWarmingUp: vi.fn(),
      chatNotif: { playPing: vi.fn(), onShareInbox: vi.fn() } as never,
      sessionInteractiveRef: { current: false }, duplexActiveRef: { current: false },
      setChatActiveLayer: vi.fn(), setProcessAlive: vi.fn(), setPendingEngineSwitch: vi.fn(),
      setEngineSwitchBusy: vi.fn(), setEngineSwitchError: vi.fn(), pendingFindQueryRef: { current: null },
      setFindInput: vi.fn(), setFindQuery: vi.fn(), setFindBarOpen: vi.fn(), setDriveRefusal: vi.fn(),
    }), { wrapper })
    hook.result.current.latestRef.current = {
      interactive: { resetSession } as never, draftKey: 'c1', refetchChats: vi.fn() as never,
    }
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })
    return { ws, invalidate, resetSession, hook }
  }

  const frame = (over: Record<string, unknown>) => ({
    type: WIRE.CHAT_MOVED, chat_id: 'c1', move_id: 'mv-1', memory: 'files', new_target: 'm-attic',
    label: 'Attic PC', from_label: 'OtoDock platform', message: '', ok: true, ...over,
  })
  const resumes = (ws: FakeWebSocket) => ws.sent.map((s) => JSON.parse(s)).filter((m) => m.type === OUT.RESUME_CHAT)

  it("a refused pick goes, and an existing chat's warmup keeps the pick", () => {
    const { ws, hook } = mountStream('')
    act(() => { hook.result.current.pickNewChatPlace('dev', 'm-attic') })
    act(() => { ws.onmessage?.({ data: JSON.stringify({
      type: WIRE.WARMUP_STARTED, chat_id: 'c-old', agent: 'dev' }) }) })
    expect(hook.result.current.newChatPlace).toEqual({ agent: 'dev', target: 'm-attic' })
    act(() => { ws.onmessage?.({ data: JSON.stringify({
      type: WIRE.WARMUP_FAILED, reason: 'target_unavailable', error: "You can't start this chat there." }) }) })
    expect(hook.result.current.newChatPlace).toBeNull()
  })

  it("a new chat's pick rides its first warmup, once, for its own agent", () => {
    const { ws, hook } = mountStream('')
    const warmups = () => ws.sent.map((m) => JSON.parse(m)).filter((m) => m.type === OUT.WARMUP)
    act(() => { hook.result.current.pickNewChatPlace('dev', 'm-attic') })
    expect(hook.result.current.newChatPlace).toEqual({ agent: 'dev', target: 'm-attic' })
    act(() => { hook.result.current.ws.warmup('other') })
    act(() => { hook.result.current.ws.warmup('dev') })
    expect(warmups().map((m) => m.target)).toEqual([undefined, 'm-attic'])
    act(() => { ws.onmessage?.({ data: JSON.stringify({
      type: WIRE.WARMUP_STARTED, chat_id: 'c9', agent: 'dev', new_chat: true }) }) })
    expect(hook.result.current.newChatPlace).toBeNull()
    act(() => { hook.result.current.ws.warmup('dev') })
    expect(warmups()[2]).not.toHaveProperty('target')
  })

  it('reads the place again and resumes the chat, quietly when the move went through', () => {
    const { ws, invalidate, resetSession } = mountStream()
    const before = resumes(ws).length
    act(() => { ws.onmessage?.({ data: JSON.stringify(frame({})) }) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: chatTargetsKey('c1') })
    expect(resetSession).toHaveBeenCalled()
    expect(resumes(ws).length).toBe(before + 1)
    expect(h.toast).not.toHaveBeenCalled()
  })

  it("says why a move did not finish, and still resumes the chat where it stayed", () => {
    const { ws, invalidate } = mountStream()
    const before = resumes(ws).length
    act(() => {
      ws.onmessage?.({ data: JSON.stringify(frame({
        ok: false, message: 'The move to Attic PC did not finish: Attic PC went offline. The chat stays on the OtoDock platform.',
      })) })
    })
    expect(h.toast).toHaveBeenCalledWith(expect.objectContaining({
      title: 'The chat was not moved',
      body: 'The move to Attic PC did not finish: Attic PC went offline. The chat stays on the OtoDock platform.',
    }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: chatTargetsKey('c1') })
    expect(resumes(ws).length).toBe(before + 1)
  })

  it("ignores a move of a chat this page does not show", () => {
    const { ws, invalidate } = mountStream()
    act(() => { ws.onmessage?.({ data: JSON.stringify(frame({ chat_id: 'c2', ok: false, message: 'x' })) }) })
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: chatTargetsKey('c1') })
    expect(h.toast).not.toHaveBeenCalled()
  })
})

describe('the task dialog', () => {
  // A user-scope task whose id holds a character a query must encode.
  const TASK = {
    id: 'task-dev-nightly-google-oauth2|1001', name: 'Nightly', agent: 'dev', scope: 'user',
    created_by: 'u1', llm_mode: 'cli', target_machine: '',
  } as Task
  const editCalls = () => h.apiFetch.mock.calls.filter(([p]) => p === `/v1/tasks/${TASK.id}/edit`)

  /** The dialog with a mode field of the page's own in its slot. */
  function TaskDialog({ onClose, onSaveExtra }: { onClose: () => void; onSaveExtra: () => Promise<void> }) {
    const [mode, setMode] = useState(TASK.llm_mode)
    return (
      <TaskSettingsModal
        agent="dev"
        task={TASK}
        onClose={onClose}
        extraFields={(
          <select aria-label="Mode" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="cli">CLI</option>
            <option value="api">API</option>
          </select>
        )}
        extraDirty={mode !== TASK.llm_mode}
        onSaveExtra={onSaveExtra}
      />
    )
  }

  async function open(onSaveExtra: () => Promise<void> = vi.fn(async () => {})) {
    const onClose = vi.fn()
    withQuery(<TaskDialog onClose={onClose} onSaveExtra={onSaveExtra} />)
    await screen.findByText("The agent's default: Attic PC")
    return onClose
  }

  const pickMode = (value: string) =>
    fireEvent.change(screen.getByRole('combobox', { name: 'Mode' }), { target: { value } })

  it("asks for the places of the task's run person", async () => {
    await open()
    expect(h.apiFetch.mock.calls.some(
      ([p]) => p === '/v1/agents/dev/run-targets?task_id=task-dev-nightly-google-oauth2%7C1001',
    )).toBe(true)
  })

  it('a change to the extra field alone saves it, busy meanwhile, then closes', async () => {
    let finish = () => {}
    const onSaveExtra = vi.fn(() => new Promise<void>((resolve) => { finish = resolve }))
    const onClose = await open(onSaveExtra)
    pickMode('api')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaveExtra).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled()
    expect(screen.getByTestId('task-runs-on')).toBeDisabled()
    expect(onClose).not.toHaveBeenCalled()
    await act(async () => { finish() })
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(editCalls()).toHaveLength(0)
  })

  it('a machine change alone saves the machine and not the extra field', async () => {
    const onSaveExtra = vi.fn(async () => {})
    const onClose = await open(onSaveExtra)
    fireEvent.change(screen.getByTestId('task-runs-on'), { target: { value: 'local' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(editCalls()).toHaveLength(1)
    expect(JSON.parse((editCalls()[0][1] as RequestInit).body as string)).toEqual({ machine: 'local' })
    expect(onSaveExtra).not.toHaveBeenCalled()
  })

  it("a refusal of the extra field shows its sentence and keeps the dialog open", async () => {
    const onClose = await open(vi.fn(async () => { throw new Error("This task's mode can't change now.") }))
    pickMode('api')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("This task's mode can't change now.")
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByText('Task settings: Nightly')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('after the extra field was refused, Save again does not send the accepted machine twice', async () => {
    const onSaveExtra = vi.fn()
      .mockRejectedValueOnce(new Error("This task's mode can't change now."))
      .mockResolvedValueOnce(undefined)
    const onClose = await open(onSaveExtra)
    fireEvent.change(screen.getByTestId('task-runs-on'), { target: { value: 'local' } })
    pickMode('api')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("This task's mode can't change now.")
    expect(editCalls()).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(onSaveExtra).toHaveBeenCalledTimes(2)
    expect(editCalls()).toHaveLength(1)
  })
})

describe("the task dialog's mode", () => {
  const TASK = {
    id: 'task-auto', name: 'Nightly', agent: 'dev', scope: 'user', created_by: 'u1',
    target_machine: '', permission_mode: 'autoReview',
  } as Task
  const editCalls = () => h.apiFetch.mock.calls.filter(([p]) => p === '/v1/tasks/task-auto/edit')
  const bodies = () => editCalls().map(([, init]) => JSON.parse((init as RequestInit).body as string))

  async function open(task: Task = TASK) {
    const onClose = vi.fn()
    withQuery(<TaskSettingsDialog agent="dev" task={task} onClose={onClose} />)
    await screen.findByText("The agent's default: Attic PC")
    return onClose
  }

  it("shows the task's mode and saves a change to it alone", async () => {
    const onClose = await open()
    expect(screen.getByTestId('task-mode')).toHaveValue('autoReview')
    fireEvent.change(screen.getByTestId('task-mode'), { target: { value: 'dontAsk' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(bodies()).toEqual([{ permission_mode: 'dontAsk' }])
  })

  it('saves the machine first, then the mode', async () => {
    const onClose = await open()
    fireEvent.change(screen.getByTestId('task-runs-on'), { target: { value: 'local' } })
    fireEvent.change(screen.getByTestId('task-mode'), { target: { value: 'dontAsk' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(bodies()).toEqual([{ machine: 'local' }, { permission_mode: 'dontAsk' }])
  })

  it("a refused Auto shows the server's sentence and keeps the dialog open", async () => {
    h.apiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith('/v1/agents/dev/run-targets')) return answer(200, RUN_TARGETS)
      if (path === '/v1/tasks/task-auto/edit') {
        return answer(400, { detail: "Auto needs an engine with its own review. This task's engine has none." })
      }
      return answer(404, { detail: 'Not found' })
    })
    const onClose = await open({ ...TASK, permission_mode: undefined })
    expect(screen.getByTestId('task-mode')).toHaveValue('dontAsk')
    fireEvent.change(screen.getByTestId('task-mode'), { target: { value: 'autoReview' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("This task's engine has none.")
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe("the platform's resume after the agent moved its chat", () => {
  const wake = (source?: string) => render(
    <BlockRenderer
      block={{ type: 'schedulewake', prompt: 'You now run on drill-sat. Continue the work.', ...(source ? { source } : {}) }}
      blockId="m1-b0" blockOrder={0} isUserMessage={false} onPermissionRespond={() => {}}
    />,
  )

  it('reads "Continued after the move" with the nudge', () => {
    wake(WAKE_SOURCE.MOVE)
    const row = screen.getByTestId('move-resume-row')
    expect(row).toHaveTextContent('Continued after the move')
    expect(row).toHaveTextContent('You now run on drill-sat. Continue the work.')
    expect(screen.queryByText('Scheduled wake')).toBeNull()
  })

  it("a schedule's own wake keeps its row", () => {
    wake()
    expect(screen.getByText('Scheduled wake')).toBeInTheDocument()
    expect(screen.queryByTestId('move-resume-row')).toBeNull()
  })
})
