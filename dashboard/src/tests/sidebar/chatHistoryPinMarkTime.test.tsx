import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

// ─── The mark's other placement: on the time line, right after the time ─────

vi.mock('@/lib/chat/pinMark', () => ({ PIN_MARK_AT: 'time' }))
vi.mock('@/hooks/useActiveChats', () => ({ useActiveChats: () => [] }))
vi.mock('@/components/chat/ActiveChatsPanel', () => ({ default: () => null }))
vi.mock('@/api/remoteMachines', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/remoteMachines')>(),
  useChatTargets: () => ({ data: undefined }),
}))
vi.mock('@/api/chats', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/chats')>(),
  useDeleteChat: () => ({ mutate: vi.fn() }),
  useRenameChat: () => ({ mutate: vi.fn() }),
  useSetChatPinned: () => ({ mutate: vi.fn() }),
  useSearchChats: () => ({ data: null, isFetching: false }),
}))

import ChatHistory from '@/components/chat/ChatHistory'
import type { Chat } from '@/api/chats'

describe('PIN_MARK_AT time', () => {
  it('puts the mark after the time, not on the title line', () => {
    const now = new Date().toISOString()
    const pinned = {
      id: 'c2', user_sub: 'u', agent: 'dev', title: 'Plan the release', session_id: null,
      permission_mode: 'default', execution_path: '', created_at: now, updated_at: now,
      can_pin: true, pinned: true,
    } as Chat
    render(<ChatHistory chats={[pinned]} activeChatId={null} agentName="dev"
                        onSelect={() => {}} onNew={() => {}} />)
    const mark = screen.getByTestId('pin-mark')
    expect(screen.getByText('Plan the release').closest('p')!.contains(mark)).toBe(false)
    expect(mark.parentElement!.textContent).toBe('now')
    expect(mark.className).toContain('text-tertiary')
  })
})
