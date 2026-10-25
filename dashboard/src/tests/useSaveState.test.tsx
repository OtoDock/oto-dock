import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({ toast: vi.fn() }))
vi.mock('@/lib/appToasts', () => ({ emitAppToast: h.toast, onAppToast: () => () => {} }))

import * as authApi from '../api/auth'
import { SaveState } from '../components/ui/SettingsControls'
import { useSaveState, type SaveStateApi } from '../hooks/useSaveState'
import { AppearanceSection, NotificationsSection } from '../pages/UserSettings.general'

// A save the test settles by hand.
function deferred() {
  let resolve!: () => void
  let reject!: (e: unknown) => void
  const promise = new Promise<void>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

let api: SaveStateApi
function Owner() {
  api = useSaveState()
  return (
    <>
      <p>Alerts <SaveState state={api.stateOf('alerts')} /></p>
      <p>Theme <SaveState state={api.stateOf('theme')} /></p>
    </>
  )
}
const shown = () => screen.getAllByRole('status').map((s) => s.textContent)

beforeEach(() => {
  vi.useFakeTimers()
  h.toast.mockClear()
})
afterEach(() => {
  vi.useRealTimers()
})

// Lets the promise callbacks run without moving the clock.
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

describe('useSaveState', () => {
  it('shows nothing for a fast save, Saved once it lands, and nothing two seconds later', async () => {
    render(<Owner />)
    const save = deferred()
    let done: Promise<boolean> | undefined
    act(() => { done = api.run('alerts', () => save.promise, { failTitle: 'The alert setting was not saved' }) })
    act(() => { vi.advanceTimersByTime(200) })
    expect(shown()).toEqual(['', ''])
    save.resolve()
    await flush()
    expect(await done).toBe(true)
    expect(shown()).toEqual(['Saved', ''])
    act(() => { vi.advanceTimersByTime(1999) })
    expect(shown()).toEqual(['Saved', ''])
    act(() => { vi.advanceTimersByTime(1) })
    expect(shown()).toEqual(['', ''])
    act(() => { vi.advanceTimersByTime(400) })
    expect(shown()).toEqual(['', ''])
    expect(h.toast).not.toHaveBeenCalled()
  })

  it('says Saving… once a save runs past 300 ms', async () => {
    render(<Owner />)
    const save = deferred()
    act(() => { void api.run('alerts', () => save.promise, { failTitle: 'x' }) })
    act(() => { vi.advanceTimersByTime(299) })
    expect(shown()).toEqual(['', ''])
    act(() => { vi.advanceTimersByTime(1) })
    expect(shown()).toEqual(['Saving…', ''])
    save.resolve()
    await flush()
    expect(shown()).toEqual(['Saved', ''])
  })

  it('a failure puts the control back and toasts its reason, and never shows Saving… later', async () => {
    render(<Owner />)
    const revert = vi.fn()
    const save = deferred()
    let done: Promise<boolean> | undefined
    act(() => { done = api.run('alerts', () => save.promise, { failTitle: 'The alert setting was not saved', revert }) })
    save.reject(new Error('The server is busy.'))
    await flush()
    expect(await done).toBe(false)
    expect(revert).toHaveBeenCalledTimes(1)
    expect(h.toast).toHaveBeenCalledWith({
      title: 'The alert setting was not saved', body: 'The server is busy.', severity: 'warning',
    })
    act(() => { vi.advanceTimersByTime(1000) })
    expect(shown()).toEqual(['', ''])
  })

  it('hands the failure to onFail instead of the toast', async () => {
    render(<Owner />)
    const onFail = vi.fn()
    act(() => { void api.run('alerts', () => Promise.reject(new Error('No.')), { onFail }) })
    await flush()
    expect(onFail).toHaveBeenCalledWith(new Error('No.'))
    expect(h.toast).not.toHaveBeenCalled()
  })

  it('takes a thunk that throws as a failure', async () => {
    render(<Owner />)
    act(() => { void api.run('alerts', () => { throw new Error('Bad input.') }, { failTitle: 'x' }) })
    await flush()
    expect(h.toast).toHaveBeenCalledTimes(1)
  })

  it('shows one indication per owner: a newer save takes the place of an older one', async () => {
    render(<Owner />)
    const first = deferred()
    const second = deferred()
    act(() => { void api.run('alerts', () => first.promise, { failTitle: 'x' }) })
    first.resolve()
    await flush()
    expect(shown()).toEqual(['Saved', ''])
    act(() => { void api.run('theme', () => second.promise, { failTitle: 'x' }) })
    expect(shown()).toEqual(['', ''])
    second.resolve()
    await flush()
    expect(shown()).toEqual(['', 'Saved'])
  })

  it('an older save that lands late changes nothing', async () => {
    render(<Owner />)
    const older = deferred()
    const newer = deferred()
    act(() => { void api.run('alerts', () => older.promise, { failTitle: 'x' }) })
    act(() => { void api.run('theme', () => newer.promise, { failTitle: 'x' }) })
    act(() => { vi.advanceTimersByTime(300) })
    expect(shown()).toEqual(['', 'Saving…'])
    older.resolve()
    await flush()
    expect(shown()).toEqual(['', 'Saving…'])
  })

  it('an older failure of the same field after a newer start neither reverts nor toasts', async () => {
    render(<Owner />)
    const revert = vi.fn()
    const older = deferred()
    const newer = deferred()
    act(() => { void api.run('alerts', () => older.promise, { failTitle: 'x', revert }) })
    act(() => { void api.run('alerts', () => newer.promise, { failTitle: 'x', revert }) })
    older.reject(new Error('late'))
    await flush()
    expect(revert).not.toHaveBeenCalled()
    expect(h.toast).not.toHaveBeenCalled()
    newer.resolve()
    await flush()
    expect(shown()).toEqual(['Saved', ''])
  })

  it('an older failure of another field still reverts and toasts it, leaving the newer one shown', async () => {
    render(<Owner />)
    const revert = vi.fn()
    const older = deferred()
    const newer = deferred()
    act(() => { void api.run('alerts', () => older.promise, { failTitle: 'The alert setting was not saved', revert }) })
    act(() => { void api.run('theme', () => newer.promise, { failTitle: 'x' }) })
    act(() => { vi.advanceTimersByTime(300) })
    older.reject(new Error('No.'))
    await flush()
    expect(revert).toHaveBeenCalledTimes(1)
    expect(h.toast).toHaveBeenCalledTimes(1)
    expect(shown()).toEqual(['', 'Saving…'])
  })

  it('items saved apart take a key each: an older refusal of one still reverts and toasts, the group shows the newer', async () => {
    let group: SaveStateApi['stateOf'] = () => null
    function List() {
      api = useSaveState()
      group = api.stateOf
      return <p>Agents <SaveState state={api.stateOf('agents')} /></p>
    }
    render(<List />)
    const revertX = vi.fn()
    const x = deferred()
    const y = deferred()
    act(() => { void api.run('agents:x', () => x.promise, { failTitle: 'The choice for x was not saved', revert: revertX }) })
    act(() => { void api.run('agents:y', () => y.promise, { failTitle: 'x' }) })
    x.reject(new Error('Refused.'))
    await flush()
    expect(revertX).toHaveBeenCalledTimes(1)
    expect(h.toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'The choice for x was not saved' }))
    y.resolve()
    await flush()
    expect(screen.getByRole('status')).toHaveTextContent('Saved')
    expect(group('agents')).toBe('saved')
    expect(group('agents:y')).toBe('saved')
    expect(group('agents:x')).toBe(null)
    // A key that only starts with the group's word is not of it.
    expect(group('agent')).toBe(null)
  })

  it('a second save of the same field during the first one\'s two seconds empties the region, then says Saved again for two seconds', async () => {
    render(<Owner />)
    act(() => { void api.run('alerts', () => Promise.resolve(), { failTitle: 'x' }) })
    await flush()
    expect(shown()).toEqual(['Saved', ''])
    act(() => { vi.advanceTimersByTime(1500) })
    const second = deferred()
    act(() => { void api.run('alerts', () => second.promise, { failTitle: 'x' }) })
    expect(shown()).toEqual(['', ''])
    act(() => { vi.advanceTimersByTime(600) })
    expect(shown()).toEqual(['Saving…', ''])
    second.resolve()
    await flush()
    expect(shown()).toEqual(['Saved', ''])
    act(() => { vi.advanceTimersByTime(1999) })
    expect(shown()).toEqual(['Saved', ''])
    act(() => { vi.advanceTimersByTime(1) })
    expect(shown()).toEqual(['', ''])
  })

  it('a refusal that lands after the owner went still reverts and toasts', async () => {
    const { unmount } = render(<Owner />)
    const revert = vi.fn()
    const save = deferred()
    act(() => { void api.run('alerts', () => save.promise, { failTitle: 'The alert setting was not saved', revert }) })
    act(() => { void api.run('theme', () => Promise.resolve(), { failTitle: 'x' }) })
    unmount()
    save.reject(new Error('Refused.'))
    await flush()
    expect(revert).toHaveBeenCalledTimes(1)
    expect(h.toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'The alert setting was not saved' }))
    expect(vi.getTimerCount()).toBe(0)
  })

  it('markSaved during a pending run of the same field takes its place: the run neither shows nor reverts', async () => {
    render(<Owner />)
    const revert = vi.fn()
    const pending = deferred()
    act(() => { void api.run('alerts', () => pending.promise, { failTitle: 'x', revert }) })
    act(() => { vi.advanceTimersByTime(300) })
    expect(shown()).toEqual(['Saving…', ''])
    act(() => { api.markSaved('alerts') })
    expect(shown()).toEqual(['Saved', ''])
    pending.reject(new Error('Refused.'))
    await flush()
    expect(revert).not.toHaveBeenCalled()
    expect(h.toast).not.toHaveBeenCalled()
    expect(shown()).toEqual(['Saved', ''])
    act(() => { vi.advanceTimersByTime(2000) })
    expect(shown()).toEqual(['', ''])
  })

  it('markSaved shows Saved at once for two seconds', () => {
    render(<Owner />)
    act(() => { api.markSaved('theme') })
    expect(shown()).toEqual(['', 'Saved'])
    act(() => { vi.advanceTimersByTime(2000) })
    expect(shown()).toEqual(['', ''])
  })

  it('keeps run the same across renders, so an effect that lists it never fires twice', () => {
    render(<Owner />)
    const first = api.run
    act(() => { api.markSaved('alerts') })
    expect(api.run).toBe(first)
  })

  it('works under StrictMode, whose second mount runs the effects again', async () => {
    function Probe() {
      api = useSaveState()
      return <SaveState state={api.stateOf('k')} />
    }
    render(<StrictMode><Probe /></StrictMode>)
    const save = deferred()
    act(() => { void api.run('k', () => save.promise, { failTitle: 'x' }) })
    save.resolve()
    await flush()
    expect(screen.getByRole('status')).toHaveTextContent('Saved')
  })

  it('clears its timer when the owner goes', async () => {
    const { unmount } = render(<Owner />)
    const save = deferred()
    act(() => { void api.run('alerts', () => save.promise, { failTitle: 'x' }) })
    unmount()
    expect(vi.getTimerCount()).toBe(0)
    save.resolve()
    await flush()
    expect(vi.getTimerCount()).toBe(0)
  })
})

// On a page, with the real API hooks: the alerts box of User Settings,
// General. Its value is the server's, so Saved shows with the refetched value
// and a refused save leaves the box as the server has it.
describe('on a settings page', () => {
  const fetchSpy = vi.spyOn(authApi, 'apiFetch')
  let stored = true
  let refuse = false
  const reply = (body: unknown, status = 200) => ({ ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) }) as Response

  beforeEach(() => {
    vi.useRealTimers()
    stored = true
    refuse = false
    fetchSpy.mockImplementation(async (_path: string, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        if (refuse) return reply({ detail: 'The server is busy.' }, 503)
        stored = JSON.parse(String(init.body)).subscription_usage_alerts
        return reply({})
      }
      return reply({ subscription_usage_alerts: stored })
    })
  })
  afterEach(() => fetchSpy.mockReset())

  const page = (ui: React.ReactNode) => render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>,
  )

  it('says Saved beside the row once the new value is on screen', async () => {
    page(<NotificationsSection />)
    const box = await screen.findByRole('checkbox', { name: 'Subscription usage alerts' })
    await waitFor(() => expect(box).toBeChecked())
    fireEvent.click(box)
    await waitFor(() => expect(screen.getAllByRole('status').some((r) => r.textContent === 'Saved')).toBe(true))
    expect(box).not.toBeChecked()
    expect(h.toast).not.toHaveBeenCalled()
  })

  it('a refused save toasts its reason and the box keeps the server\'s value', async () => {
    refuse = true
    page(<NotificationsSection />)
    const box = await screen.findByRole('checkbox', { name: 'Subscription usage alerts' })
    await waitFor(() => expect(box).toBeChecked())
    fireEvent.click(box)
    // The server's sentence is the toast's body.
    await waitFor(() => expect(h.toast).toHaveBeenCalledWith(expect.objectContaining({
      title: 'The alert setting was not saved', body: 'The server is busy.',
    })))
    expect(box).toBeChecked()
    expect(screen.getAllByRole('status').every((r) => r.textContent !== 'Saved')).toBe(true)
  })

  it('a preference kept on this device shows nothing', () => {
    page(<AppearanceSection />)
    fireEvent.click(screen.getByRole('button', { name: /Dark/ }))
    expect(screen.queryAllByRole('status').filter((r) => /Sav/.test(r.textContent ?? ''))).toEqual([])
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
