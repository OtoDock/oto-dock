import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderWithDialogs } from './dialogsHarness'
import MobileAppsCard from '@/pages/admin/MobileAppsCard'

// ─── System Settings, Mobile apps: the store rating ask saves at once, the
//     days when the field is left, and a refused value goes back. ─────────

const h = vi.hoisted(() => ({
  savePlatform: vi.fn(),
  settings: { store_review_enabled: true, store_review_days: 90, forced_keys: [] as string[] },
}))

vi.mock('@/pages/admin/PlatformPage.hooks', () => ({
  usePlatformSettings: () => ({ data: h.settings, isLoading: false }),
  useSavePlatformSettings: () => ({ mutateAsync: h.savePlatform, isPending: false }),
}))

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(<QueryClientProvider client={qc}><MobileAppsCard /></QueryClientProvider>)
}

beforeEach(() => {
  h.savePlatform.mockReset().mockResolvedValue({})
  h.settings = { store_review_enabled: true, store_review_days: 90, forced_keys: [] }
})

describe('the Mobile apps card', () => {
  it('turns the ask off at once', async () => {
    mount()
    const box = screen.getByLabelText('Ask for a store rating') as HTMLInputElement
    expect(box.checked).toBe(true)
    await act(async () => { fireEvent.click(box) })
    expect(h.savePlatform).toHaveBeenCalledWith({ store_review_enabled: false })
  })

  it('saves the days when the field is left, and only a change', async () => {
    mount()
    const input = screen.getByLabelText('Days before the second ask') as HTMLInputElement
    expect(input.value).toBe('90')
    await act(async () => { fireEvent.blur(input) })
    expect(h.savePlatform).not.toHaveBeenCalled()
    fireEvent.change(input, { target: { value: '120' } })
    await act(async () => { fireEvent.blur(input) })
    expect(h.savePlatform).toHaveBeenCalledWith({ store_review_days: '120' })
  })

  it('puts the stored days back when the server refuses them', async () => {
    h.savePlatform.mockRejectedValue(new Error('The days before the second ask are a whole number from 30 to 730.'))
    mount()
    const input = screen.getByLabelText('Days before the second ask') as HTMLInputElement
    fireEvent.change(input, { target: { value: '10' } })
    await act(async () => { fireEvent.blur(input) })
    await waitFor(() => expect(input.value).toBe('90'))
  })

  it('the days wait while the ask is off, and forced values cannot change', () => {
    h.settings = { store_review_enabled: false, store_review_days: 45, forced_keys: ['store_review_enabled'] }
    mount()
    expect((screen.getByLabelText('Ask for a store rating') as HTMLInputElement).disabled).toBe(true)
    const input = screen.getByLabelText('Days before the second ask') as HTMLInputElement
    expect(input.value).toBe('45')
    expect(input.disabled).toBe(true)
  })
})
