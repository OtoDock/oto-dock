// The Languages card's row of candidates: a click shows at once and builds on
// the previous click (the server's copy arrives only after the refetch), the
// boxes are disabled while a save is pending or the settings load, and a
// failed save returns to the server's list.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'

const api = vi.hoisted(() => ({
  settings: { data: { language_candidates: 'en,el,de,es,fr,it' } as { language_candidates: string } | undefined, isLoading: false },
  mutateAsync: vi.fn((_vars: unknown) => Promise.resolve()),
  isPending: false,
}))

vi.mock('@/api/audio', () => ({
  useAudioSettings: () => api.settings,
  useSaveAudioSettings: () => ({ mutateAsync: api.mutateAsync, isPending: api.isPending }),
}))

import { LanguageCandidatesRow } from '@/components/audio/LanguageCandidatesRow'

const box = (view: ReturnType<typeof render>, name: string) =>
  view.getByLabelText(name) as HTMLInputElement

describe('LanguageCandidatesRow', () => {
  beforeEach(() => {
    api.settings = { data: { language_candidates: 'en,el,de,es,fr,it' }, isLoading: false }
    api.mutateAsync = vi.fn((_vars: unknown) => Promise.resolve())
    api.isPending = false
  })

  it('shows a click at once and a second click builds on the first', () => {
    const view = render(<LanguageCandidatesRow />)
    fireEvent.click(box(view, 'Greek'))
    expect(api.mutateAsync).toHaveBeenLastCalledWith({ language_candidates: 'en,de,es,fr,it' })
    expect(box(view, 'Greek').checked).toBe(false)         // not snapped back to the stale list
    fireEvent.click(box(view, 'German'))
    expect(api.mutateAsync).toHaveBeenLastCalledWith({ language_candidates: 'en,es,fr,it' })
    expect(box(view, 'German').checked).toBe(false)
    // The server's copy catches up: the local list is dropped without a flash.
    api.settings = { data: { language_candidates: 'en,es,fr,it' }, isLoading: false }
    view.rerender(<LanguageCandidatesRow />)
    expect(box(view, 'Greek').checked).toBe(false)
  })

  it('a failed save returns to the server\'s list', async () => {
    api.mutateAsync = vi.fn((_vars: unknown) => Promise.reject(new Error('refused')))
    const view = render(<LanguageCandidatesRow />)
    await act(async () => { fireEvent.click(box(view, 'Greek')) })
    expect(api.mutateAsync).toHaveBeenCalledTimes(1)
    expect(box(view, 'Greek').checked).toBe(true)
  })

  it('the boxes are disabled while the settings load or a save is pending', () => {
    api.settings = { data: undefined, isLoading: true }
    let view = render(<LanguageCandidatesRow />)
    expect(box(view, 'Greek').disabled).toBe(true)
    fireEvent.click(box(view, 'Greek'))
    expect(api.mutateAsync).not.toHaveBeenCalled()
    view.unmount()
    api.settings = { data: { language_candidates: 'en,el' }, isLoading: false }
    api.isPending = true
    view = render(<LanguageCandidatesRow />)
    expect(box(view, 'English').disabled).toBe(true)
    expect(box(view, 'German').checked).toBe(false)
  })

  it('the last language cannot be unticked', () => {
    api.settings = { data: { language_candidates: 'el' }, isLoading: false }
    const view = render(<LanguageCandidatesRow />)
    fireEvent.click(box(view, 'Greek'))
    expect(api.mutateAsync).not.toHaveBeenCalled()
    expect(box(view, 'Greek').checked).toBe(true)
  })
})
