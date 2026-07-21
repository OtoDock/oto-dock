import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// ─── ChatHistory task mode (the sidebar's Task history view) ────────────────

const searchMock = vi.fn(() => ({ data: null, isFetching: false }))
const taskChatsMock = vi.fn<() => unknown[]>(() => [])
const deleteMutateMock = vi.fn()
vi.mock('@/api/chats', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/api/chats')>()
  return {
    ...orig,
    useDeleteChat: () => ({ mutate: deleteMutateMock }),
    useSearchChats: (...args: unknown[]) => searchMock(...args as []),
    useTaskChats: () => ({ data: taskChatsMock() }),
  }
})

const activeRowsMock = vi.fn<() => unknown[]>(() => [])
vi.mock('@/hooks/useActiveChats', () => ({
  useActiveChats: () => activeRowsMock(),
}))

// The Active-now strip has its own tests — stub it out of this composition.
vi.mock('@/components/chat/ActiveChatsPanel', () => ({ default: () => null }))

import ChatHistory from '@/components/chat/ChatHistory'
import type { Chat, TaskChat } from '@/api/chats'
import { useChatStore } from '@/store/chatStore'

function chat(id: string, title: string, over: Partial<Chat> = {}): Chat {
  return {
    id, user_sub: 'u', agent: 'dev', title, session_id: null,
    permission_mode: 'default', execution_path: '',
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    ...over,
  } as Chat
}

function taskChat(id: string, title: string, over: Partial<TaskChat> = {}): TaskChat {
  return { ...chat(id, title), run_status: 'completed', task_name: 't-nightly', ...over }
}

function renderHistory(over: Partial<Parameters<typeof ChatHistory>[0]> = {}) {
  return render(
    <ChatHistory
      chats={[chat('c1', 'Fix the tests')]}
      activeChatId={null}
      agentName="dev"
      onSelect={() => {}}
      onNew={() => {}}
      tasksMode={false}
      onTasksModeChange={() => {}}
      {...over}
    />,
  )
}

beforeEach(() => {
  searchMock.mockReturnValue({ data: null, isFetching: false })
  taskChatsMock.mockReturnValue([])
  activeRowsMock.mockReturnValue([])
  deleteMutateMock.mockReset()
  useChatStore.setState({ byChat: {} })
})

describe('ChatHistory — Task history view', () => {
  it('chat mode: "Chat history" title, toggle flips the mode', () => {
    const onChange = vi.fn()
    renderHistory({ onTasksModeChange: onChange })
    expect(screen.getByText('Chat history')).toBeTruthy()
    expect(screen.getByText('Fix the tests')).toBeTruthy()
    fireEvent.click(screen.getByTestId('tasks-toggle'))
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('task mode: "Task history" title, task rows with run subtitle', () => {
    taskChatsMock.mockReturnValue([taskChat('task-run-1', 'nightly backup sweep')])
    renderHistory({ tasksMode: true })
    expect(screen.getByText('Task history')).toBeTruthy()
    // Chat title rides in the subtitle (task_name owns the title slot).
    expect(screen.getByText(/nightly backup sweep/)).toBeTruthy()
    expect(screen.getByText('t-nightly')).toBeTruthy()
    expect(screen.queryByText('Fix the tests')).toBeNull()
    // Tasks are fire-and-forget: no per-row options/delete menu.
    expect(screen.queryByTitle('Options')).toBeNull()
  })

  it('task rows are titled by the task name, chat title in the subtitle', () => {
    taskChatsMock.mockReturnValue([
      taskChat('task-run-7', 'prompt first line', { task_name: 'Nightly report' }),
      taskChat('task-run-8', 'one-off prompt line', { task_name: undefined }),
    ])
    renderHistory({ tasksMode: true })
    // Named run: the NAME is the row title, the per-run chat title (prompt
    // first line / LLM upgrade) moves to the subtitle.
    expect(screen.getByText('Nightly report')).toBeTruthy()
    expect(screen.getByText(/prompt first line/)).toBeTruthy()
    // Run whose task row is gone (one-time cleanup): chat title stays.
    expect(screen.getByText('one-off prompt line')).toBeTruthy()
  })

  it('+ New Chat stays in task mode; clicking it exits to the chat view', () => {
    const onChange = vi.fn()
    const onNew = vi.fn()
    renderHistory({ tasksMode: true, onTasksModeChange: onChange, onNew })
    fireEvent.click(screen.getByText('+ New Chat'))
    expect(onChange).toHaveBeenCalledWith(false)
    expect(onNew).toHaveBeenCalled()
  })

  it('a running task row pulses purple (run_status seed, no store slice)', () => {
    taskChatsMock.mockReturnValue([taskChat('task-run-2', 'busy task', { run_status: 'running' })])
    renderHistory({ tasksMode: true })
    const row = screen.getByTitle('Task running…')
    expect(row.className).toContain('oto-row-live-purple')
    // Plain scheduled tasks carry NO left accent — the rail is role-based.
    expect(row.className).toContain('border-l-transparent')
  })

  it('left accents are role-based, same rule as chat rows', () => {
    taskChatsMock.mockReturnValue([
      taskChat('task-run-5', 'delegated lane', { origin: 'delegated' }),
      taskChat('task-run-6', 'plain schedule'),
    ])
    renderHistory({ tasksMode: true })
    // Chat titles ride in the subtitle line (task_name owns the title slot).
    const delegated = screen.getByText(/delegated lane/).closest('div.group') as HTMLElement
    const plain = screen.getByText(/plain schedule/).closest('div.group') as HTMLElement
    expect(delegated.className).toContain('violet')
    expect(plain.className).not.toContain('violet')
    expect(plain.className).toContain('border-l-transparent')
  })

  it('day-group headers render in task mode', () => {
    taskChatsMock.mockReturnValue([taskChat('task-run-7', 'fresh run')])
    renderHistory({ tasksMode: true })
    expect(screen.getByText('Today')).toBeTruthy()
  })

  it('the toggle pulses purple while the agent has active tasks and mode is off', () => {
    activeRowsMock.mockReturnValue([
      { id: 'task-run-3', agent: 'dev', title: 'x', phase: 'streaming', sourceType: 'task' },
    ])
    renderHistory()
    expect(screen.getByTestId('tasks-toggle').className).toContain('animate-pulse')
    // Foreign-agent tasks don't pulse this agent's toggle.
    activeRowsMock.mockReturnValue([
      { id: 'task-run-4', agent: 'other', title: 'x', phase: 'streaming', sourceType: 'task' },
    ])
    renderHistory()
    const toggles = screen.getAllByTestId('tasks-toggle')
    expect(toggles[toggles.length - 1].className).not.toContain('animate-pulse')
  })

  it('task-mode search passes kind=tasks', () => {
    renderHistory({ tasksMode: true })
    expect(searchMock).toHaveBeenCalledWith('dev', '', 'tasks')
  })
})

describe('ChatHistory — delete failure surfacing', () => {
  function confirmDeleteFirstRow() {
    fireEvent.click(screen.getByTitle('Options'))
    fireEvent.click(screen.getByText('Delete'))
    // Menu is closed now — the only remaining Delete button is the confirm.
    fireEvent.click(screen.getByText('Delete'))
  }

  it('a failed delete shows the backend error inline', () => {
    deleteMutateMock.mockImplementation((_id: string, opts?: { onError?: (e: Error) => void }) => {
      opts?.onError?.(new Error('Failed to delete chat (HTTP 500)'))
    })
    renderHistory()
    confirmDeleteFirstRow()
    expect(deleteMutateMock).toHaveBeenCalledWith('c1', expect.anything())
    expect(screen.getByText('Failed to delete chat (HTTP 500)')).toBeTruthy()
    // Dismissable.
    fireEvent.click(screen.getByLabelText('Dismiss'))
    expect(screen.queryByText('Failed to delete chat (HTTP 500)')).toBeNull()
  })

  it('a successful delete shows nothing', () => {
    renderHistory()
    confirmDeleteFirstRow()
    expect(deleteMutateMock).toHaveBeenCalled()
    expect(screen.queryByLabelText('Dismiss')).toBeNull()
  })
})
