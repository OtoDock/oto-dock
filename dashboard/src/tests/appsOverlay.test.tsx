import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { PinnedApp } from '@/api/apps'

// The overlay is about strip/approval composition here — stub the sandboxed
// frame and the registry hook.
vi.mock('@/components/apps/AppFrame', () => ({
  default: ({ app }: { app: { slug: string } }) => (
    <div data-testid="app-frame">{app.slug}</div>
  ),
}))
const appsData = vi.hoisted(() => ({ current: [] as unknown[] }))
vi.mock('@/api/apps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/apps')>()),
  useApps: () => ({ data: appsData.current, isLoading: false }),
}))

import AppsOverlay from '@/components/apps/AppsOverlay'

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

function renderOverlay() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AppsOverlay agent="dev" />
    </QueryClientProvider>,
  )
}

describe('AppsOverlay — chip strip visibility', () => {
  it('a single PERSONAL app renders frame-only (no tab chrome)', () => {
    appsData.current = [mkApp('brief', { scope: 'personal' })]
    renderOverlay()
    expect(screen.getByTestId('app-frame')).toBeTruthy()
    // No chip for the app — the strip is hidden entirely; the owner keeps
    // the overlay ✕ (2026-08-15 — removal is no longer agent-mediated only).
    expect(screen.queryByRole('button', { name: 'brief' })).toBeNull()
    expect(screen.getByLabelText('Remove brief')).toBeTruthy()
  })

  it('two apps bring the strip back', () => {
    appsData.current = [mkApp('brief'), mkApp('ops')]
    renderOverlay()
    expect(screen.getByRole('button', { name: 'brief' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'ops' })).toBeTruthy()
  })

  it('a single SHARED app hides the strip too — removal moves to the overlay ✕', () => {
    appsData.current = [mkApp('team')]
    renderOverlay()
    expect(screen.getByTestId('app-frame')).toBeTruthy()
    // No chip — but the frame carries the small overlay ✕.
    expect(screen.queryByRole('button', { name: 'team' })).toBeNull()
    expect(screen.getByLabelText('Remove team')).toBeTruthy()
  })

  it('the overlay ✕ is absent with 2+ apps or when hidden apps keep the strip', () => {
    appsData.current = [mkApp('brief'), mkApp('ops')]
    const { unmount } = renderOverlay()
    // Strip owns removal — the active chip's ✕, not an overlay.
    expect(screen.getByRole('button', { name: 'brief' })).toBeTruthy()
    expect(screen.getAllByLabelText(/Remove /)).toHaveLength(1)
    unmount()
    appsData.current = [mkApp('team'), mkApp('parked', { hidden_for_me: true })]
    renderOverlay()
    // One visible app but hidden ones exist → strip stays (restore chips
    // need a home) and the chip ✕ is the entry point.
    expect(screen.getByRole('button', { name: 'team' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '+1 hidden' })).toBeTruthy()
  })

  it('a solo personal app shows no overlay ✕ when the viewer cannot manage it', () => {
    appsData.current = [mkApp('brief', { scope: 'personal', can_manage: false })]
    renderOverlay()
    expect(screen.getByTestId('app-frame')).toBeTruthy()
    expect(screen.queryByLabelText('Remove brief')).toBeNull()
  })
})

describe('AppsOverlay — per-user hide of shared pins (S2)', () => {
  it('a viewer gets Hide-for-me but no team-wide unpin', () => {
    appsData.current = [mkApp('team', { can_manage: false, can_approve: false })]
    renderOverlay()
    fireEvent.click(screen.getByLabelText('Remove team'))
    expect(screen.getByRole('button', { name: 'Hide for me' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Unpin/ })).toBeNull()
  })

  it('an editor gets BOTH Hide-for-me and Unpin-for-everyone', () => {
    appsData.current = [mkApp('team')]
    renderOverlay()
    fireEvent.click(screen.getByLabelText('Remove team'))
    expect(screen.getByRole('button', { name: 'Hide for me' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Unpin for everyone' })).toBeTruthy()
  })

  it('a personal row keeps the single Unpin (no hide split)', () => {
    appsData.current = [mkApp('mine', { scope: 'personal' }), mkApp('team')]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: 'mine' }))
    fireEvent.click(screen.getByLabelText('Remove mine'))
    expect(screen.getByRole('button', { name: 'Unpin' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Hide for me' })).toBeNull()
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
