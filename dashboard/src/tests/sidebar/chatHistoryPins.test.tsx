import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// ─── ChatHistory: a person's pinned chats (the menu, the toggle, the mark) ───

const searchMock = vi.fn((..._args: unknown[]) => ({ data: null, isFetching: false }))
const pinMutateMock = vi.fn()
vi.mock('@/api/chats', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/api/chats')>()
  return {
    ...orig,
    useDeleteChat: () => ({ mutate: vi.fn() }),
    useRenameChat: () => ({ mutate: vi.fn() }),
    useSetChatPinned: () => ({ mutate: pinMutateMock }),
    useSearchChats: (...args: unknown[]) => searchMock(...args),
  }
})
vi.mock('@/hooks/useActiveChats', () => ({ useActiveChats: () => [] }))
vi.mock('@/components/chat/ActiveChatsPanel', () => ({ default: () => null }))
vi.mock('@/api/remoteMachines', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/remoteMachines')>(),
  useChatTargets: () => ({ data: undefined }),
}))

import ChatHistory from '@/components/chat/ChatHistory'
import type { Chat } from '@/api/chats'
import { PINNED_VIEW } from '@/api/chats'
import { useChatStore } from '@/store/chatStore'

function chat(id: string, title: string, over: Partial<Chat> = {}): Chat {
  return {
    id, user_sub: 'u', agent: 'dev', title, session_id: null,
    permission_mode: 'default', execution_path: '',
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    can_pin: true, pinned: false, ...over,
  } as Chat
}

function renderHistory(over: Partial<Parameters<typeof ChatHistory>[0]> = {}) {
  return render(
    <ChatHistory
      chats={[chat('c1', 'Fix the tests'), chat('c2', 'Plan the release', { pinned: true })]}
      activeChatId={null}
      agentName="dev"
      onSelect={() => {}}
      onNew={() => {}}
      tasksMode={false}
      onTasksModeChange={() => {}}
      pinnedView={false}
      onPinnedViewChange={() => {}}
      {...over}
    />,
  )
}

function openMenu(title: string) {
  const row = screen.getByText(title).closest('.group') as HTMLElement
  fireEvent.click(row.querySelector('[title="Options"]') as HTMLElement)
}

beforeEach(() => {
  searchMock.mockReturnValue({ data: null, isFetching: false })
  pinMutateMock.mockReset()
  useChatStore.setState({ byChat: {} })
})

describe('the row menu', () => {
  it('offers "Pin chat" on a chat that is not pinned and pins it', () => {
    renderHistory()
    openMenu('Fix the tests')
    fireEvent.click(screen.getByText('Pin chat'))
    expect(pinMutateMock.mock.calls[0][0]).toEqual({ chatId: 'c1', pinned: true })
  })

  it('offers "Unpin chat" on a pinned chat and unpins it', () => {
    renderHistory()
    openMenu('Plan the release')
    expect(screen.queryByText('Pin chat')).toBeNull()
    fireEvent.click(screen.getByText('Unpin chat'))
    expect(pinMutateMock.mock.calls[0][0]).toEqual({ chatId: 'c2', pinned: false })
  })

  it('shows the menu for a row whose only item is the pin', () => {
    renderHistory({ chats: [chat('c3', 'A worker', { can_rename: false, can_delete: false })] })
    openMenu('A worker')
    expect(screen.getByText('Pin chat')).toBeTruthy()
    expect(screen.queryByText('Delete')).toBeNull()
  })

  it('offers no pin on a row the server does not let this person pin', () => {
    renderHistory({ chats: [chat('c4', 'Not mine', { can_pin: false })] })
    openMenu('Not mine')
    expect(screen.queryByText('Pin chat')).toBeNull()
  })
})

describe('the toggle and the view', () => {
  it('sits left of the task toggle and turns the view on', () => {
    const onChange = vi.fn()
    renderHistory({ onPinnedViewChange: onChange })
    const toggle = screen.getByTestId('pinned-toggle')
    expect(toggle.getAttribute('title')).toBe('Show pinned chats')
    expect(toggle.getAttribute('aria-pressed')).toBe('false')
    const buttons: Element[] = Array.from(toggle.parentElement!.querySelectorAll('button'))
    expect(buttons.indexOf(toggle)).toBeLessThan(buttons.indexOf(screen.getByTestId('tasks-toggle')))
    fireEvent.click(toggle)
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('is drawn only by a server that serves pins, and never in the task view', () => {
    const { unmount } = renderHistory({
      chats: [{ ...chat('c1', 'Old server'), can_pin: undefined, pinned: undefined }] })
    expect(screen.queryByTestId('pinned-toggle')).toBeNull()
    unmount()
    renderHistory({ tasksMode: true, taskChats: [] })
    expect(screen.queryByTestId('pinned-toggle')).toBeNull()
  })

  it('on: the pinned chats, their words, and the pinned search', () => {
    renderHistory({ pinnedView: true, pinnedChats: [chat('c2', 'Plan the release', { pinned: true })] })
    const toggle = screen.getByTestId('pinned-toggle')
    expect(toggle.getAttribute('title')).toBe('Show all chats')
    expect(toggle.getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByText('Pinned chats')).toBeTruthy()
    expect(screen.getByPlaceholderText('Search pinned chats...')).toBeTruthy()
    expect(screen.getByText('Plan the release')).toBeTruthy()
    expect(screen.queryByText('Fix the tests')).toBeNull()
    expect(searchMock.mock.calls[searchMock.mock.calls.length - 1]?.[2]).toBe(PINNED_VIEW)
  })

  it('on with no pins: "No pinned chats"', () => {
    renderHistory({ pinnedView: true, pinnedChats: [] })
    expect(screen.getByText('No pinned chats')).toBeTruthy()
    expect(screen.getByTestId('pinned-toggle')).toBeTruthy()
  })

  it('off: the history as it was', () => {
    renderHistory()
    expect(screen.getByText('Chat history')).toBeTruthy()
    expect(screen.getByPlaceholderText('Search chats...')).toBeTruthy()
    expect(screen.getByText('Fix the tests')).toBeTruthy()
    expect(searchMock.mock.calls[searchMock.mock.calls.length - 1]?.[2]).toBe('chats')
  })
})

describe('the mark', () => {
  it('sits in front of the title in the grey of the time line, on pinned rows only', () => {
    renderHistory()
    const marks = screen.getAllByTestId('pin-mark')
    expect(marks).toHaveLength(1)
    const mark = marks[0]
    expect(mark.getAttribute('title')).toBe('Pinned')
    expect(mark.className).toContain('text-tertiary')
    const titleLine = screen.getByText('Plan the release').closest('p')!
    expect(titleLine.contains(mark)).toBe(true)
  })

  it('takes the fill\'s foreground on the open row', () => {
    renderHistory({ activeChatId: 'c2' })
    expect(screen.getByTestId('pin-mark').className).toContain('text-action-fg')
  })

  it('leaves the delegation edge and the unread dot in place', () => {
    const lane = { delegate_role: 'worker', unread: true, last_response_at: new Date().toISOString() }
    renderHistory({ chats: [chat('c2', 'A lane', { ...lane, pinned: true })] })
    const row = screen.getByText('A lane').closest('.group') as HTMLElement
    const plain = render(
      <ChatHistory chats={[chat('c2', 'A lane', lane)]} activeChatId={null}
                   agentName="dev" onSelect={() => {}} onNew={() => {}} />,
    ).container.querySelector('.group') as HTMLElement
    expect(row.className).toBe(plain.className)
    expect(row.querySelector('[title="New response, not opened yet"]')).toBeTruthy()
  })
})
