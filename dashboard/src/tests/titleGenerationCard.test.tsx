/**
 * Setup → Chat Title Generation tells "configured" from "working": a
 * provider whose credential does not resolve (a hosted-relay row with no
 * account behind it) read Active while no title was ever made. The last
 * attempt now says which it is, and Check asks the provider now.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { TitleGeneration } from '@/api/titleGeneration'

const h = vi.hoisted(() => ({
  status: null as TitleGeneration | null,
  check: vi.fn(),
}))
vi.mock('@/api/titleGeneration', () => ({
  useTitleGeneration: () => ({ data: h.status, isLoading: false }),
  useSaveTitleGeneration: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve()), isPending: false }),
  useCheckTitleGeneration: () => ({ mutate: h.check, isPending: false, isError: false, error: null }),
}))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: { role: 'admin', sub: 'a' } }) }))

import { ChatTitleGenerationCard } from '@/pages/admin/SystemSettingsTab'

const base: TitleGeneration = {
  enabled: true, selected_model: '', active: true, active_provider: 'groq',
  active_model: 'openai/gpt-oss-120b', options: [], last_attempt: null,
}

function card() {
  const qc = new QueryClient()
  return render(<QueryClientProvider client={qc}><ChatTitleGenerationCard /></QueryClientProvider>)
}

describe('Chat Title Generation card', () => {
  it('says Not working with the reason when the last attempt failed', () => {
    h.status = { ...base, last_attempt: { ok: false, provider: 'groq', model: 'openai/gpt-oss-120b',
      reason: "the provider's credential did not resolve (the hosted relay gave no token, or the subscription holds no key)",
      at: '2026-10-09T00:00:00Z' } }
    card()
    expect(screen.getByText('Not working')).toBeTruthy()
    expect(screen.getByTestId('title-gen-last').textContent).toContain(
      "Titles are not being made: the provider's credential did not resolve")
    fireEvent.click(screen.getByRole('button', { name: 'Check' }))
    expect(h.check).toHaveBeenCalled()
  })

  it('says Active when the last title came through, and before any attempt', () => {
    h.status = { ...base, last_attempt: { ok: true, kind: 'title', provider: 'groq', model: 'm', reason: '', at: '' } }
    const first = card()
    expect(screen.getByText('Active')).toBeTruthy()
    expect(screen.getByTestId('title-gen-last').textContent).toBe('The last title came through.')
    first.unmount()
    // A Check proves the credential only, and says so.
    h.status = { ...base, last_attempt: { ok: true, kind: 'credential', provider: 'groq', model: 'm', reason: 'the credential resolves', at: '' } }
    const checked = card()
    expect(screen.getByTestId('title-gen-last').textContent).toBe('The credential resolves. No title made since the server started.')
    checked.unmount()
    h.status = { ...base }
    card()
    expect(screen.getByText('Active')).toBeTruthy()
    expect(screen.getByTestId('title-gen-last').textContent).toContain('No title tried since the server started')
  })

  it('says a pin no model row lists any more runs on Auto', () => {
    h.status = { ...base, unlisted_pin: 'claude-haiku-4-5' }
    const first = card()
    expect(screen.getByTestId('title-gen-unlisted').textContent).toBe(
      'The pinned model claude-haiku-4-5 is no longer offered. Titles use Auto.')
    first.unmount()
    h.status = { ...base }
    card()
    expect(screen.queryByTestId('title-gen-unlisted')).toBeNull()
  })

  it('stays Inactive with nothing configured, no Check', () => {
    h.status = { ...base, active: false, active_provider: '', active_model: '' }
    card()
    expect(screen.getByText('Inactive')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Check' })).toBeNull()
  })
})
