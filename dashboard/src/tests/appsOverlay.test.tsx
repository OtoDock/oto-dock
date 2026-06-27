import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
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

function mkApp(slug: string): PinnedApp {
  return {
    id: `id-${slug}`, slug, title: slug, scope: 'shared', position: 0,
    rel_path: `workspace/apps/${slug}.html`, updated_at: '', actions: [],
    actions_sig: '', actions_approved: true, approval_stale: false,
    can_approve: true, can_manage: true,
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
  it('a single app renders frame-only (no tab chrome)', () => {
    appsData.current = [mkApp('brief')]
    renderOverlay()
    expect(screen.getByTestId('app-frame')).toBeTruthy()
    // No chip for the app — the strip is hidden entirely.
    expect(screen.queryByRole('button', { name: /brief/ })).toBeNull()
  })

  it('two apps bring the strip back', () => {
    appsData.current = [mkApp('brief'), mkApp('ops')]
    renderOverlay()
    expect(screen.getByRole('button', { name: 'brief' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'ops' })).toBeTruthy()
  })
})
