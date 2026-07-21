import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

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
    useSearchChats: (...args: unknown[]) => searchMock(...args as []),
    useTaskChats: () => ({ data: [] }),
  }
})

vi.mock('@/hooks/useActiveChats', () => ({ useActiveChats: () => [] }))

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
  pinnedLabel: 'local sandbox',
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
  useChatStore.setState({ byChat: {} })
})

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
    expect(screen.getByText('Runs on local sandbox — move to Attic PC')).toBeTruthy()
  })

  it('clicking the row opens the confirm; "Move chat" fires onMoveChat', () => {
    seedSlice('c1')
    const onMoveChat = vi.fn()
    renderHistory({ onMoveChat })
    fireEvent.click(screen.getByTitle('Options'))
    fireEvent.click(screen.getByText('Runs on local sandbox — move to Attic PC'))
    expect(screen.getByText('Move chat to Attic PC?')).toBeTruthy()
    fireEvent.click(screen.getByText('Move chat'))
    expect(onMoveChat).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Move chat to Attic PC?')).toBeNull()
  })

  it('Cancel closes the confirm without firing the op', () => {
    seedSlice('c1')
    const onMoveChat = vi.fn()
    renderHistory({ onMoveChat })
    fireEvent.click(screen.getByTitle('Options'))
    fireEvent.click(screen.getByText('Runs on local sandbox — move to Attic PC'))
    fireEvent.click(screen.getByText('Cancel'))
    expect(onMoveChat).not.toHaveBeenCalled()
    expect(screen.queryByText('Move chat to Attic PC?')).toBeNull()
  })

  it('the move row is disabled while that chat streams', () => {
    seedSlice('c1', 'streaming')
    renderHistory()
    fireEvent.click(screen.getByTitle('Options'))
    const row = screen.getByText('Runs on local sandbox — move to Attic PC') as HTMLButtonElement
    expect(row.disabled).toBe(true)
  })

  it('a NON-open chat with mismatch data gets a plain info row (no move action)', () => {
    seedSlice('c1')
    renderHistory({ activeChatId: null })
    fireEvent.click(screen.getByTitle('Options'))
    expect(screen.getByText('Runs on local sandbox')).toBeTruthy()
    expect(screen.queryByText(/move to Attic PC/)).toBeNull()
  })
})
