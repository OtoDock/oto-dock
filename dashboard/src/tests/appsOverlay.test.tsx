import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'

import type { PinnedApp } from '@/api/apps'

// The overlay is about strip/approval composition here — stub the sandboxed
// frame and the registry hook. The stub numbers its mounts, so a test can
// tell a fresh frame from a reused instance.
const frameMounts = vi.hoisted(() => ({ n: 0 }))
vi.mock('@/components/apps/AppFrame', async () => {
  const { useState } = await import('react')
  return {
    default: ({ app }: { app: { slug: string } }) => {
      const [mount] = useState(() => ++frameMounts.n)
      return <div data-testid="app-frame" data-mount={mount}>{app.slug}</div>
    },
  }
})
const appsData = vi.hoisted(() => ({ current: [] as unknown[] }))
// The rollback mutation: the host passes per-call callbacks, a test fires them.
const rollbackMutate = vi.hoisted(() => vi.fn())
vi.mock('@/api/apps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/apps')>()),
  useApps: () => ({ data: appsData.current, isLoading: false }),
  useRollbackApp: () => ({ mutate: rollbackMutate, isPending: false }),
}))
vi.mock('@/components/sharing/SharePopover', () => ({
  default: ({ app }: { app: { id: string } }) => <div data-testid="share-popover">{app.id}</div>,
}))

import AppsOverlay from '@/components/apps/AppsOverlay'
import { rollbackNoticeText } from '@/api/apps'

// jsdom has no ResizeObserver (the strip's fade-edge recompute).
class RO {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= RO

function mkApp(slug: string, over: Partial<PinnedApp> = {}): PinnedApp {
  return {
    id: `id-${slug}`, slug, title: slug, scope: 'shared', position: 0,
    rel_path: `workspace/apps/${slug}.html`, updated_at: '', actions: [],
    actions_sig: '', actions_approved: true, approval_stale: false,
    can_approve: true, can_manage: true, hidden_for_me: false, ...over,
  }
}

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

function renderOverlay(path = '/chat/dev') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <AppsOverlay agent="dev" />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const openMenu = (title: string) => fireEvent.click(screen.getByLabelText(`Options for ${title}`))
const loc = () => screen.getByTestId('loc').textContent

describe('AppsOverlay — chip strip visibility', () => {
  it('a single PERSONAL app renders frame-only (no tab chrome)', () => {
    appsData.current = [mkApp('brief', { scope: 'personal' })]
    renderOverlay()
    expect(screen.getByTestId('app-frame')).toBeTruthy()
    // No chip for the app — the strip is hidden entirely; the menu sits in
    // the frame's corner.
    expect(screen.queryByRole('button', { name: 'brief' })).toBeNull()
    expect(screen.getByLabelText('Options for brief')).toBeTruthy()
  })

  it('two apps bring the strip back', () => {
    appsData.current = [mkApp('brief'), mkApp('ops')]
    renderOverlay()
    expect(screen.getByRole('button', { name: 'brief' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'ops' })).toBeTruthy()
  })

  it('a single SHARED app hides the strip too — the menu moves to the corner', () => {
    appsData.current = [mkApp('team')]
    renderOverlay()
    expect(screen.getByTestId('app-frame')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'team' })).toBeNull()
    expect(screen.getByLabelText('Options for team')).toBeTruthy()
  })

  it('the menu lives on the ACTIVE chip with 2+ apps or when hidden apps keep the strip', () => {
    appsData.current = [mkApp('brief'), mkApp('ops')]
    const { unmount } = renderOverlay()
    expect(screen.getByRole('button', { name: 'brief' })).toBeTruthy()
    expect(screen.getAllByLabelText(/Options for /)).toHaveLength(1)
    expect(screen.getByLabelText('Options for brief')).toBeTruthy()
    unmount()
    appsData.current = [mkApp('team'), mkApp('parked', { hidden_for_me: true })]
    renderOverlay()
    // One visible app but hidden ones exist → strip stays (restore chips
    // need a home) and the chip carries the menu.
    expect(screen.getByRole('button', { name: 'team' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '+1 hidden' })).toBeTruthy()
    expect(screen.getAllByLabelText(/Options for /)).toHaveLength(1)
  })

  it('the menu opens full screen, and Share opens the popover for whoever may manage the row', () => {
    appsData.current = [mkApp('brief')]
    const { unmount } = renderOverlay()
    openMenu('brief')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Share' }))
    expect(screen.getByTestId('share-popover').textContent).toBe('id-brief')
    openMenu('brief')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Open full screen' }))
    expect(loc()).toBe('/apps/id-brief')
    unmount()
    // A viewer of the row (or a grantee) has no Share; a granted personal
    // app can be hidden from the viewer's own list.
    appsData.current = [mkApp('theirs', { scope: 'personal', can_manage: false, granted: true })]
    renderOverlay()
    openMenu('theirs')
    expect(screen.queryByRole('menuitem', { name: 'Share' })).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Hide for me' })).toBeTruthy()
  })

  it('offers Roll back only while a previous release exists', () => {
    appsData.current = [mkApp('brief', { has_previous_release: true })]
    const { unmount } = renderOverlay()
    openMenu('brief')
    expect(screen.getByRole('menuitem', { name: 'Roll back' })).toBeTruthy()
    unmount()
    appsData.current = [mkApp('brief', { has_previous_release: false })]
    renderOverlay()
    openMenu('brief')
    expect(screen.queryByRole('menuitem', { name: 'Roll back' })).toBeNull()
  })

  it('the active tab rides ?app= — read on arrival, written on switch', () => {
    appsData.current = [mkApp('brief'), mkApp('ops')]
    renderOverlay('/chat/dev?app=id-ops')
    expect(screen.getByTestId('app-frame').textContent).toBe('ops')
    fireEvent.click(screen.getByRole('button', { name: 'brief' }))
    expect(screen.getByTestId('app-frame').textContent).toBe('brief')
    expect(loc()).toBe('/chat/dev?app=id-brief')
  })

  it('Roll back asks first, then reports which release serves and what happened to the data', () => {
    rollbackMutate.mockClear()
    appsData.current = [mkApp('board', { kind: 'folder', release: 4, has_previous_release: true })]
    renderOverlay()
    openMenu('board')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Roll back' }))
    const confirm = screen.getByTestId('app-rollback-confirm')
    expect(confirm.textContent).toContain('Roll back “board” to the previous release?')
    expect(confirm.textContent).toContain('Its data goes back to before this release')
    // Cancel: nothing moves.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByTestId('app-rollback-confirm')).toBeNull()
    expect(rollbackMutate).not.toHaveBeenCalled()
    // Confirm: one mutation, and the result lands on the card slot.
    openMenu('board')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Roll back' }))
    fireEvent.click(screen.getByRole('button', { name: 'Roll back' }))
    expect(rollbackMutate).toHaveBeenCalledTimes(1)
    const [id, callbacks] = rollbackMutate.mock.calls[0]
    expect(id).toBe('id-board')
    act(() => { callbacks.onSuccess({ release: 3, screens: 2, db_restored: true, snapshot: 'rollback-x.sqlite' }) })
    const notice = screen.getByTestId('app-rollback-notice')
    expect(notice.textContent).toContain('Release 3 serves again.')
    expect(notice.textContent).toContain('restored from before release 4')
    fireEvent.click(screen.getByLabelText('Dismiss'))
    expect(screen.queryByTestId('app-rollback-notice')).toBeNull()
    // A refusal shows its reason the same way.
    openMenu('board')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Roll back' }))
    fireEvent.click(screen.getByRole('button', { name: 'Roll back' }))
    act(() => { rollbackMutate.mock.calls[1][1].onError(new Error('a release is waiting for approval')) })
    expect(screen.getByTestId('app-rollback-notice').textContent).toContain('Roll back failed: a release is waiting for approval')
  })

  it('the rollback sentence has three outcomes', () => {
    const file = mkApp('brief')
    const folder = mkApp('board', { kind: 'folder' })
    expect(rollbackNoticeText(file, 2, { release: 1, screens: 0 })).toBe('Release 1 serves again.')
    expect(rollbackNoticeText(folder, 5, { release: 4, screens: 1, db_restored: true, snapshot: 's' }))
      .toContain('restored from before release 5')
    expect(rollbackNoticeText(folder, 5, { release: 4, screens: 1, db_restored: false }))
      .toContain('No copy from before release 5 exists, so the app keeps its current data.')
  })

  it('a tab switch mounts a fresh frame — one instance never serves two apps', () => {
    appsData.current = [mkApp('brief'), mkApp('ops', { kind: 'folder', release_sha: 'abc' })]
    renderOverlay()
    const first = screen.getByTestId('app-frame').getAttribute('data-mount')
    fireEvent.click(screen.getByRole('button', { name: 'ops' }))
    const second = screen.getByTestId('app-frame').getAttribute('data-mount')
    expect(screen.getByTestId('app-frame').textContent).toBe('ops')
    expect(second).not.toBe(first)
    // Back again: another fresh instance, not the first one revived.
    fireEvent.click(screen.getByRole('button', { name: 'brief' }))
    expect(screen.getByTestId('app-frame').getAttribute('data-mount')).not.toBe(first)
  })
})

describe('AppsOverlay — per-user hide of shared pins (S2)', () => {
  it('a viewer gets Hide-for-me but no team-wide unpin', () => {
    appsData.current = [mkApp('team', { can_manage: false, can_approve: false })]
    renderOverlay()
    openMenu('team')
    expect(screen.getByRole('menuitem', { name: 'Hide for me' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: /Unpin/ })).toBeNull()
  })

  it('an editor gets BOTH Hide-for-me and Unpin-for-everyone, unpin behind a confirm', () => {
    appsData.current = [mkApp('team')]
    renderOverlay()
    openMenu('team')
    expect(screen.getByRole('menuitem', { name: 'Hide for me' })).toBeTruthy()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Unpin for everyone' }))
    expect(screen.getByText(/Unpin “team” for everyone\?/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Unpin for everyone' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()
  })

  it('a personal row keeps the single Unpin (no hide entry)', () => {
    appsData.current = [mkApp('mine', { scope: 'personal' }), mkApp('team')]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: 'mine' }))
    openMenu('mine')
    expect(screen.getByRole('menuitem', { name: 'Unpin' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: 'Hide for me' })).toBeNull()
  })

  it('hidden rows leave the strip and surface under "+N hidden" → restore', () => {
    appsData.current = [mkApp('team'), mkApp('parked', { hidden_for_me: true })]
    renderOverlay()
    // Parked chip is off the strip…
    expect(screen.queryByRole('button', { name: 'parked' })).toBeNull()
    // …and the hidden affordance expands to a restore chip.
    fireEvent.click(screen.getByRole('button', { name: '+1 hidden' }))
    expect(screen.getByRole('button', { name: /parked ↺/ })).toBeTruthy()
  })

  it('all shared apps hidden → restore chips render in the empty state', () => {
    appsData.current = [mkApp('parked', { hidden_for_me: true })]
    renderOverlay()
    expect(screen.getByText('All shared apps are hidden')).toBeTruthy()
    expect(screen.getByRole('button', { name: /parked ↺/ })).toBeTruthy()
  })
})
