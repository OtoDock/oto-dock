// The chat audio policy: a refused save shows the server's refusal as a
// toast (the master switch and the voice mode alike), and the two values that
// need a platform speech provider are offered only while one is usable.
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { platformProviderUsable, type AudioProvider } from '@/api/audio'

const h = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  policy: 'native_preferred' as string,
  providers: undefined as unknown[] | undefined,
}))
vi.mock('@/api/audio', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/audio')>()),
  useAudioPolicy: () => ({ data: { chat_enabled: true, chat_user_policy: h.policy, show_experimental: false }, isLoading: false }),
  useUpdateAudioPolicy: () => ({ mutateAsync: h.mutateAsync, isPending: false }),
  useAudioProviders: () => ({ data: h.providers }),
}))

import { ChatPolicyRadio } from '@/components/audio/ChatPolicyRadio'
import { renderWithDialogs } from './dialogsHarness'

const provider = (over: Partial<AudioProvider> = {}): AudioProvider => ({
  id: 1, provider_type: 'stt', provider_name: 'deepgram', label: '', credential_key: 'audio-deepgram',
  enabled_for_calls: true, enabled_for_chat: true, is_default_calls: false, is_default_chat: true,
  voices: {}, advanced: {}, credential_configured: true, advanced_defaults: {}, ...over,
})

const labels = () => screen.getAllByRole('button').map(b => b.textContent ?? '').filter(t => /^(Native|Platform|User)/.test(t)).map(t => t.split(/The |Each /)[0])

describe('the chat audio policy', () => {
  it('says so when the server refuses a change', async () => {
    h.providers = [provider()]
    h.mutateAsync.mockRejectedValue(new Error('Only an admin may change this'))
    renderWithDialogs(<ChatPolicyRadio />)
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'Chat audio' })) })
    const region = screen.getByRole('region', { name: 'Notifications' })
    await waitFor(() => expect(region.textContent).toContain('Chat audio was not switched'))
    expect(region.textContent).toContain('Only an admin may change this')
    await act(async () => { fireEvent.click(screen.getByText("User's choice")) })
    await waitFor(() => expect(region.textContent).toContain('The voice setting was not saved'))
  })

  it('offers Platform only and User\'s choice only with a usable chat provider', () => {
    h.providers = [provider()]
    const { unmount } = renderWithDialogs(<ChatPolicyRadio />)
    expect(labels()).toEqual(['Native only', 'Native preferred', 'Platform only', "User's choice"])
    expect(screen.getByText(/Safari, iPhones, iPads and Automatic dictation/)).toBeInTheDocument()
    unmount()

    // A default whose credential is missing is not usable, and a usable one
    // that is not the chat default does not count.
    h.providers = [provider({ credential_configured: false }), provider({ id: 2, is_default_chat: false })]
    const second = renderWithDialogs(<ChatPolicyRadio />)
    expect(labels()).toEqual(['Native only', 'Native preferred'])
    expect(screen.getByText(/Add a speech provider below/)).toBeInTheDocument()
    second.unmount()

    // A local engine needs no credential.
    h.providers = [provider({ credential_key: null, credential_configured: false })]
    const third = renderWithDialogs(<ChatPolicyRadio />)
    expect(labels()).toHaveLength(4)
    third.unmount()
  })

  it('keeps the stored value on offer without a provider, and every option while the list loads', () => {
    h.providers = []
    h.policy = 'user_choice'
    const { unmount } = renderWithDialogs(<ChatPolicyRadio />)
    expect(labels()).toEqual(['Native only', 'Native preferred', "User's choice"])
    unmount()
    h.policy = 'native_preferred'
    h.providers = undefined
    renderWithDialogs(<ChatPolicyRadio />)
    expect(labels()).toHaveLength(4)
    expect(platformProviderUsable(undefined)).toBeUndefined()
  })
})
