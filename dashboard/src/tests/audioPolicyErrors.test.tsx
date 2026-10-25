// The chat audio policy saves show the server's refusal as a toast (they
// said nothing before): the master switch and the voice mode alike.
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({ mutateAsync: vi.fn() }))
vi.mock('@/api/audio', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/audio')>()),
  useAudioPolicy: () => ({ data: { chat_enabled: true, chat_user_policy: 'native_preferred', show_experimental: false }, isLoading: false }),
  useUpdateAudioPolicy: () => ({ mutateAsync: h.mutateAsync, isPending: false }),
}))

import { ChatPolicyRadio } from '@/components/audio/ChatPolicyRadio'
import { renderWithDialogs } from './dialogsHarness'

describe('the chat audio policy', () => {
  it('says so when the server refuses a change', async () => {
    h.mutateAsync.mockRejectedValue(new Error('Only an admin may change this'))
    renderWithDialogs(<ChatPolicyRadio />)
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'Chat audio' })) })
    const region = screen.getByRole('region', { name: 'Notifications' })
    await waitFor(() => expect(region.textContent).toContain('Chat audio was not switched'))
    expect(region.textContent).toContain('Only an admin may change this')
    await act(async () => { fireEvent.click(screen.getByText('User choice')) })
    await waitFor(() => expect(region.textContent).toContain('The voice setting was not saved'))
  })
})
