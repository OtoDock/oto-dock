import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

// ─── A search result shows its match and opens the chat there ───────────────

const searchMock = vi.fn(() => ({ data: null as unknown, isFetching: false }))
vi.mock('@/api/chats', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/chats')>(),
  useDeleteChat: () => ({ mutate: vi.fn() }),
  useRenameChat: () => ({ mutate: vi.fn() }),
  useSearchChats: () => searchMock(),
}))
vi.mock('@/hooks/useActiveChats', () => ({ useActiveChats: () => [] }))
vi.mock('@/components/chat/ActiveChatsPanel', () => ({ default: () => null }))
vi.mock('@/api/remoteMachines', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/remoteMachines')>(),
  useChatTargets: () => ({ data: undefined }),
}))

import ChatHistory from '@/components/chat/ChatHistory'
import { SNIPPET_MARK_END, SNIPPET_MARK_START, type Chat, type TaskChat } from '@/api/chats'
import { useChatStore } from '@/store/chatStore'

function chat(id: string, over: Partial<Chat> = {}): Chat {
  return {
    id, user_sub: 'u', agent: 'dev', title: `Chat ${id}`, session_id: null,
    permission_mode: 'default', execution_path: '',
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(), ...over,
  } as Chat
}

const marked = (w: string) => `${SNIPPET_MARK_START}${w}${SNIPPET_MARK_END}`

async function searchFor(q: string) {
  fireEvent.change(screen.getByPlaceholderText(/Search (chats|tasks)\.\.\./), { target: { value: q } })
  await act(async () => { vi.advanceTimersByTime(400) })
}

beforeEach(() => {
  vi.useFakeTimers()
  useChatStore.setState({ byChat: {} })
})
afterEach(() => vi.useRealTimers())

describe('ChatHistory search results', () => {
  it('shows the words around the match with the matched ones highlighted', async () => {
    searchMock.mockReturnValue({
      data: [chat('s1', { match_message_id: 41, match_snippet: `the ${marked('zeppelin')} manifest went missing` })],
      isFetching: false,
    })
    render(<ChatHistory chats={[chat('c1')]} activeChatId={null} agentName="dev" onSelect={() => {}} onNew={() => {}} />)
    await searchFor('zeppelin')
    const line = screen.getByTestId('search-snippet')
    expect(line.textContent).toBe('the zeppelin manifest went missing')
    expect(line.querySelector('mark')?.textContent).toBe('zeppelin')
  })

  it('opens a result at its matching message, and a title match without one', async () => {
    searchMock.mockReturnValue({
      data: [chat('s1', { match_message_id: 41, match_snippet: marked('x') }), chat('s2', { title: 'Zeppelin plans' })],
      isFetching: false,
    })
    const onSelect = vi.fn()
    render(<ChatHistory chats={[]} activeChatId={null} agentName="dev" onSelect={onSelect} onNew={() => {}} />)
    await searchFor('zeppelin')
    fireEvent.click(screen.getByText('Chat s1'))
    expect(onSelect).toHaveBeenLastCalledWith('s1', 'zeppelin', 41)
    fireEvent.click(screen.getByText('Plans', { exact: false }))
    expect(onSelect).toHaveBeenLastCalledWith('s2', 'zeppelin', undefined)
    expect(screen.getAllByTestId('search-snippet')).toHaveLength(1)
  })

  it('does the same for task runs', async () => {
    searchMock.mockReturnValue({
      data: [{ ...chat('task-r1'), task_name: 'nightly', match_message_id: 7, match_snippet: marked('backup') } as TaskChat],
      isFetching: false,
    })
    const onSelect = vi.fn()
    render(<ChatHistory chats={[]} taskChats={[]} tasksMode activeChatId={null} agentName="dev"
      onSelect={onSelect} onNew={() => {}} onTasksModeChange={() => {}} />)
    await searchFor('backup')
    expect(screen.getByTestId('search-snippet').textContent).toBe('backup')
    fireEvent.click(screen.getByText('nightly'))
    expect(onSelect).toHaveBeenLastCalledWith('task-r1', 'backup', 7)
  })

  it('shows no snippet once the search is cleared', async () => {
    searchMock.mockReturnValue({ data: null, isFetching: false })
    render(<ChatHistory chats={[chat('c1', { match_snippet: marked('stale') })]} activeChatId={null}
      agentName="dev" onSelect={() => {}} onNew={() => {}} />)
    expect(screen.queryByTestId('search-snippet')).toBeNull()
  })
})
