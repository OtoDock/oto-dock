import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { personClick } from './dialogsHarness'

// ─── ChatHistory kebab — the target-mismatch move row ───────────────────────
// The banner's permanent home after dismissal: rendered only when the chat's
// store slice carries mismatch data. Actionable (move + confirm) only for
// the OPEN chat — the move_chat op acts on the connection's open chat — a
// non-open row states the pin as a plain info row.

const searchMock = vi.fn(() => ({ data: null, isFetching: false }))
vi.mock('@/api/chats', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/api/chats')>()
  return {
    ...orig,
    useDeleteChat: () => ({ mutate: vi.fn() }),
    useRenameChat: () => ({ mutate: vi.fn() }),
    useRenameTask: () => ({ mutate: vi.fn() }),
    useSearchChats: (...args: unknown[]) => searchMock(...args as []),
    useTaskChats: () => ({ data: [] }),
  }
})

vi.mock('@/hooks/useActiveChats', () => ({ useActiveChats: () => [] }))

// The OPEN chat's places (the "Runs on" chip's query): asked for the open
// row only, never per row.
const targetsMock = vi.fn((_chatId: string | null): { data: unknown } => ({ data: undefined }))
vi.mock('@/api/remoteMachines', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/remoteMachines')>(),
  useChatTargets: (chatId: string | null) => targetsMock(chatId),
}))

// The Active-now strip has its own tests — stub it out of this composition.
vi.mock('@/components/chat/ActiveChatsPanel', () => ({ default: () => null }))

import ChatHistory from '@/components/chat/ChatHistory'
import type { Chat } from '@/api/chats'
import { useChatStore, type ChatSlice, type ChatStreamPhase } from '@/store/chatStore'

function chat(id: string, title: string): Chat {
  return {
    id, user_sub: 'u', agent: 'dev', title, session_id: null,
    permission_mode: 'default', execution_path: '',
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  } as Chat
}

const MISMATCH = {
  pinnedTarget: 'local',
  pinnedLabel: 'the OtoDock platform',
  resolvedTarget: 'm-attic',
  resolvedLabel: 'Attic PC',
}

function seedSlice(chatId: string, status: ChatStreamPhase = 'ready') {
  const slice: ChatSlice = {
    chatId,
    status,
    agent: 'dev',
    executionPath: 'claude-code-cli',
    executionTarget: 'local',
    fallbackReason: null,
    targetMismatch: MISMATCH,
    warmupStartedAt: null,
    warmupError: null,
    lastEventAt: 0,
    draftInput: '',
    queuedMessages: [],
    pendingImages: [],
    pendingFiles: [],
  }
  useChatStore.setState({ byChat: { [chatId]: slice } })
}

function renderHistory(over: Partial<Parameters<typeof ChatHistory>[0]> = {}) {
  return render(
    <ChatHistory
      chats={[chat('c1', 'Fix the tests')]}
      activeChatId="c1"
      agentName="dev"
      onSelect={() => {}}
      onNew={() => {}}
      onMoveChat={() => {}}
      {...over}
    />,
  )
}

beforeEach(() => {
  searchMock.mockReturnValue({ data: null, isFetching: false })
  targetsMock.mockReset()
  targetsMock.mockReturnValue({ data: undefined })
  useChatStore.setState({ byChat: {} })
})

/** The open chat's targets answer, its place chosen or not. */
function targetsAnswer(placeChosen: boolean, currentReachable = true) {
  return {
    current: { target: 'm-old', label: 'Old box', pairing: 'user', state: currentReachable ? 'online' : 'disconnected', owner: '' },
    default_target: 'm-attic',
    targets: [
      { target: 'm-old', label: 'Old box', pairing: 'user', state: currentReachable ? 'online' : 'disconnected',
        reachable: currentReachable, is_default: false, is_current: true, pickable: true, why_not: '', allowed: true },
    ],
    can_move: true, refusal: '', lock: null, moving: null, place_chosen: placeChosen,
  }
}

describe('ChatHistory — target-mismatch move row', () => {
  it('no mismatch data → the dropdown has only Delete', () => {
    renderHistory()
    fireEvent.click(screen.getByTitle('Options'))
    expect(screen.getByText('Delete')).toBeTruthy()
    expect(screen.queryByText(/Runs on/)).toBeNull()
  })

  it('mismatch on the OPEN chat → actionable move row', () => {
    seedSlice('c1')
    renderHistory()
    fireEvent.click(screen.getByTitle('Options'))
    expect(screen.getByText('Runs on the OtoDock platform. Move to Attic PC')).toBeTruthy()
  })

  it('clicking the row opens the confirm; "Move chat" fires onMoveChat', () => {
    seedSlice('c1')
    const onMoveChat = vi.fn()
    renderHistory({ onMoveChat })
    fireEvent.click(screen.getByTitle('Options'))
    fireEvent.click(screen.getByText('Runs on the OtoDock platform. Move to Attic PC'))
    expect(screen.getByText('Move this chat to Attic PC?')).toBeTruthy()
    personClick(screen.getByText('Move chat'))
    expect(onMoveChat).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Move this chat to Attic PC?')).toBeNull()
  })

  it('Cancel closes the confirm without firing the op', () => {
    seedSlice('c1')
    const onMoveChat = vi.fn()
    renderHistory({ onMoveChat })
    fireEvent.click(screen.getByTitle('Options'))
    fireEvent.click(screen.getByText('Runs on the OtoDock platform. Move to Attic PC'))
    personClick(screen.getByText('Cancel'))
    expect(onMoveChat).not.toHaveBeenCalled()
    expect(screen.queryByText('Move this chat to Attic PC?')).toBeNull()
  })

  it('a turn starting closes the move confirm for good: it does not come back when the turn ends', () => {
    seedSlice('c1')
    renderHistory()
    fireEvent.click(screen.getByTitle('Options'))
    fireEvent.click(screen.getByText('Runs on the OtoDock platform. Move to Attic PC'))
    expect(screen.getByText('Move this chat to Attic PC?')).toBeTruthy()
    act(() => { seedSlice('c1', 'streaming') })
    expect(screen.queryByText('Move this chat to Attic PC?')).toBeNull()
    act(() => { seedSlice('c1') })
    expect(screen.queryByText('Move this chat to Attic PC?')).toBeNull()
  })

  it('the move row is disabled while that chat streams', () => {
    seedSlice('c1', 'streaming')
    renderHistory()
    fireEvent.click(screen.getByTitle('Options'))
    const row = screen.getByText('Runs on the OtoDock platform. Move to Attic PC') as HTMLButtonElement
    expect(row.disabled).toBe(true)
  })

  it('the confirm names where the chat goes', () => {
    seedSlice('c1')
    renderHistory()
    fireEvent.click(screen.getByTitle('Options'))
    fireEvent.click(screen.getByText('Runs on the OtoDock platform. Move to Attic PC'))
    expect(screen.getByText('Move this chat to Attic PC?')).toBeTruthy()
    expect(screen.getByText('The chat continues on Attic PC.')).toBeTruthy()
  })

  it("the confirm says a summary goes when the open chat's machine is offline", () => {
    seedSlice('c1')
    targetsMock.mockImplementation((id) => ({ data: id ? targetsAnswer(false, false) : undefined }))
    renderHistory()
    fireEvent.click(screen.getByTitle('Options'))
    fireEvent.click(screen.getByText('Runs on the OtoDock platform. Move to Attic PC'))
    expect(screen.getByText(
      'Old box is offline, so the chat continues on Attic PC from a summary of the conversation.',
    )).toBeTruthy()
  })

  it('a place a person chose hides the move on the OPEN chat (the pin stays stated)', () => {
    seedSlice('c1')
    targetsMock.mockImplementation((id) => ({ data: id ? targetsAnswer(true) : undefined }))
    renderHistory()
    fireEvent.click(screen.getByTitle('Options'))
    expect(screen.queryByText(/Move to Attic PC/)).toBeNull()
    expect(screen.getByText('Runs on the OtoDock platform')).toBeTruthy()
  })

  it('the places are asked for the open chat only', () => {
    seedSlice('c1')
    renderHistory({ chats: [chat('c1', 'Fix the tests'), chat('c2', 'Other')] })
    const asked = new Set(targetsMock.mock.calls.map(([id]) => id))
    expect(asked).toEqual(new Set(['c1', null]))
  })

  it('a NON-open chat with mismatch data gets a plain info row (no move action)', () => {
    seedSlice('c1')
    renderHistory({ activeChatId: null })
    fireEvent.click(screen.getByTitle('Options'))
    expect(screen.getByText('Runs on the OtoDock platform')).toBeTruthy()
    expect(screen.queryByText(/Move to Attic PC/)).toBeNull()
  })
})
