import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

// ─── ChatHistory loads older rows as the last one comes near ─────────────────

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
import type { Chat, ListPaging, TaskChat } from '@/api/chats'
import { useChatStore } from '@/store/chatStore'

// The observers the list made, and the element each watches: a test plays
// the browser by calling one back.
interface Watched { cb: IntersectionObserverCallback; el?: Element; options?: IntersectionObserverInit; live: boolean }
let observers: Watched[] = []
class FakeObserver {
  w: Watched
  constructor(cb: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    this.w = { cb, options, live: true }
    observers.push(this.w)
  }
  observe(el: Element) { this.w.el = el }
  disconnect() { this.w.live = false }
  unobserve() {}
  takeRecords() { return [] }
}

function nearBottom() {
  const live = observers.filter((o) => o.live)
  const w = live[live.length - 1]
  if (!w?.el) throw new Error('nothing observed')
  act(() => w.cb([{ isIntersecting: true, target: w.el } as unknown as IntersectionObserverEntry],
    w as unknown as IntersectionObserver))
}

function chat(id: string, over: Partial<Chat> = {}): Chat {
  return {
    id, user_sub: 'u', agent: 'dev', title: `Chat ${id}`, session_id: null,
    permission_mode: 'default', execution_path: '',
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(), ...over,
  } as Chat
}

function paging(over: Partial<ListPaging> = {}): ListPaging {
  return { hasMore: true, loadingOlder: false, failed: false, loadOlder: vi.fn(), ...over }
}

function renderList(over: Partial<Parameters<typeof ChatHistory>[0]> = {}) {
  return render(
    <ChatHistory
      chats={[chat('c1'), chat('c2')]}
      activeChatId={null}
      agentName="dev"
      onSelect={() => {}}
      onNew={() => {}}
      onTasksModeChange={() => {}}
      {...over}
    />,
  )
}

beforeEach(() => {
  observers = []
  vi.stubGlobal('IntersectionObserver', FakeObserver)
  searchMock.mockReturnValue({ data: null, isFetching: false })
  useChatStore.setState({ byChat: {} })
})
afterEach(() => vi.unstubAllGlobals())

describe('ChatHistory older pages', () => {
  it('asks for the next page when the sentinel comes near the list bottom', () => {
    const p = paging()
    renderList({ chatPaging: p })
    const w = observers[observers.length - 1]
    expect(w.el).toBe(screen.getByTestId('history-older'))
    expect(w.options?.rootMargin).toBe('0px 0px 400px 0px')
    expect((w.options?.root as HTMLElement | null)?.contains(w.el!)).toBe(true)
    nearBottom()
    expect(p.loadOlder).toHaveBeenCalledTimes(1)
  })

  it('shows the loading line while a page loads, and watches again when it lands', () => {
    const { rerender } = renderList({ chatPaging: paging({ loadingOlder: true }) })
    expect(screen.getByText('Loading older chats…')).toBeTruthy()
    expect(observers.filter((o) => o.live)).toHaveLength(0)
    const next = paging()
    rerender(
      <ChatHistory chats={[chat('c1'), chat('c2'), chat('c3')]} chatPaging={next} activeChatId={null}
        agentName="dev" onSelect={() => {}} onNew={() => {}} onTasksModeChange={() => {}} />,
    )
    expect(screen.queryByText('Loading older chats…')).toBeNull()
    expect(observers.filter((o) => o.live)).toHaveLength(1)
    nearBottom()
    expect(next.loadOlder).toHaveBeenCalledTimes(1)
  })

  it('shows nothing below the rows when no older rows remain', () => {
    renderList({ chatPaging: paging({ hasMore: false }) })
    expect(screen.queryByTestId('history-older')).toBeNull()
    expect(observers).toHaveLength(0)
  })

  it('after a failed page it waits for the press instead of asking again', () => {
    const p = paging({ failed: true })
    renderList({ chatPaging: p })
    expect(observers.filter((o) => o.live)).toHaveLength(0)
    fireEvent.click(screen.getByText('Load older chats'))
    expect(p.loadOlder).toHaveBeenCalledTimes(1)
  })

  it('offers the press where the browser cannot watch the list', () => {
    vi.stubGlobal('IntersectionObserver', undefined)
    const p = paging()
    renderList({ chatPaging: p })
    fireEvent.click(screen.getByText('Load older chats'))
    expect(p.loadOlder).toHaveBeenCalledTimes(1)
  })

  it('pages the task view with its own list', () => {
    const chats = paging()
    const tasks = paging()
    const runs = [{ ...chat('task-r1'), task_name: 'nightly' } as TaskChat]
    renderList({ tasksMode: true, chatPaging: chats, taskChats: runs, taskPaging: tasks })
    expect(screen.getByText('nightly')).toBeTruthy()
    nearBottom()
    expect(tasks.loadOlder).toHaveBeenCalledTimes(1)
    expect(chats.loadOlder).not.toHaveBeenCalled()
    renderList({ tasksMode: true, taskChats: runs, taskPaging: paging({ loadingOlder: true }) })
    expect(screen.getByText('Loading older task runs…')).toBeTruthy()
  })

  it('does not page while search shows its own results', async () => {
    vi.useFakeTimers()
    try {
      searchMock.mockReturnValue({ data: [chat('s1')], isFetching: false })
      const p = paging()
      renderList({ chatPaging: p })
      fireEvent.change(screen.getByPlaceholderText('Search chats...'), { target: { value: 'needle' } })
      await act(async () => { vi.advanceTimersByTime(400) })
      expect(screen.queryByTestId('history-older')).toBeNull()
      expect(observers.filter((o) => o.live)).toHaveLength(0)
    } finally {
      vi.useRealTimers()
    }
  })
})
