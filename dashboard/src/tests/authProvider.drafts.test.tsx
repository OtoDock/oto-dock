/**
 * The sign-in names whose chat drafts this browser holds: AuthProvider binds
 * the stored drafts to the person before any page renders, removes another
 * person's, and a sign-out leaves none in storage.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'

const me = vi.hoisted(() => ({ sub: 'u1' }))

vi.mock('@/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/auth')>()),
  fetchAuthConfig: vi.fn(async () => ({})),
  fetchCurrentUser: vi.fn(async () => ({ sub: me.sub, email: `${me.sub}@example.com`, name: me.sub, role: 'member' })),
  logout: vi.fn(async () => {}),
}))

import { AuthProvider } from '@/contexts/AuthProvider'
import { useAuth } from '@/contexts/AuthContext'
import { useChatStore } from '@/store/chatStore'
import { clearChatDrafts } from '@/store/chatDrafts'
import { useAgentPrefsStore } from '@/store/agentPrefsStore'

function Probe() {
  const { user, logout } = useAuth()
  return user ? <button onClick={logout}>{`signed in ${user.sub}`}</button> : null
}

async function boot(sub: string) {
  me.sub = sub
  render(<AuthProvider><Probe /></AuthProvider>)
  return screen.findByText(`signed in ${sub}`)
}

const draftKeys = () => Object.keys(localStorage).filter((k) => k.startsWith('oto-dock-draft:'))
/** The person this browser's sticky prefs name, as the last boot left it. */
const lastPerson = (sub: string) => useAgentPrefsStore.setState({ __user_sub: sub })

afterEach(() => {
  clearChatDrafts()
  useChatStore.setState({ byChat: {} })
  localStorage.clear()
})

describe('the chat drafts at sign-in and sign-out', () => {
  it('loads the person\'s drafts, removes another person\'s, and a sign-out leaves none', async () => {
    lastPerson('u1')
    localStorage.setItem('oto-dock-draft:u1:c1', 'mine')
    localStorage.setItem('oto-dock-draft:u0:c2', 'theirs')
    const button = await boot('u1')
    expect(useChatStore.getState().byChat.c1?.draftInput).toBe('mine')
    expect(useChatStore.getState().byChat.c2).toBeUndefined()
    expect(draftKeys()).toEqual(['oto-dock-draft:u1:c1'])
    act(() => { useChatStore.getState().setDraftInput('c3', 'typed') })
    act(() => { button.click() })
    expect(draftKeys()).toEqual([])
  })

  it('drops the record of an earlier version that another person left', async () => {
    lastPerson('u0')
    localStorage.setItem('oto-dock-chat-store', JSON.stringify({
      state: { byChat: { c1: { draftInput: 'theirs', queuedMessages: [] } } }, version: 3 }))
    await boot('u1')
    expect(useChatStore.getState().byChat.c1?.draftInput ?? '').toBe('')
    expect(localStorage.getItem('oto-dock-chat-store')).toBeNull()
    expect(draftKeys()).toEqual([])
  })

  it('carries the person\'s own record of an earlier version over', async () => {
    lastPerson('u1')
    localStorage.setItem('oto-dock-chat-store', JSON.stringify({
      state: { byChat: { c1: { draftInput: 'from an earlier version', queuedMessages: [] } } }, version: 3 }))
    await boot('u1')
    expect(useChatStore.getState().byChat.c1?.draftInput).toBe('from an earlier version')
    expect(localStorage.getItem('oto-dock-chat-store')).toBeNull()
  })
})
