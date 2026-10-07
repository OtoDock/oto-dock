import { describe, it, expect, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'

import type { PinnedApp } from '@/api/apps'

// The overlay is about strip/approval composition here — stub the sandboxed
// frame and the registry hook. The stub numbers its mounts, so a test can
// tell a fresh frame from a reused instance.
const frameMounts = vi.hoisted(() => ({ n: 0 }))
const frameProps = vi.hoisted(() => ({ current: null as any }))
vi.mock('@/components/apps/AppFrame', async () => {
  const { useState } = await import('react')
  return {
    default: (props: { app: { slug: string }; agent: string; onSendPrompt?: unknown }) => {
      const [mount] = useState(() => ++frameMounts.n)
      frameProps.current = props
      return <div data-testid="app-frame" data-mount={mount} data-agent={props.agent}>{props.app.slug}</div>
    },
  }
})
const appsData = vi.hoisted(() => ({ current: [] as unknown[] }))
// A desktop (hover-capable) window, so the chips render draggable and the
// placed row's exclusion is a real assertion (the overlay reads matchMedia
// once at import).
vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, onchange: null, dispatchEvent: () => false }),
  })
})
const reorderMutate = vi.hoisted(() => vi.fn())
// The rollback mutation: the host passes per-call callbacks, a test fires them.
const rollbackMutate = vi.hoisted(() => vi.fn())
const unpinMutate = vi.hoisted(() => vi.fn())
vi.mock('@/api/apps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/apps')>()),
  useApps: () => ({ data: appsData.current, isLoading: false }),
  useUnpinApp: () => ({ mutate: unpinMutate, isPending: false }),
  useRollbackApp: () => ({ mutate: rollbackMutate, isPending: false }),
  useReorderApps: () => ({ mutate: reorderMutate, isPending: false }),
}))
vi.mock('@/components/sharing/SharePopover', () => ({
  default: ({ app }: { app: { id: string } }) => <div data-testid="share-popover">{app.id}</div>,
}))
// The share mutations a placed row uses (SHARING.md); the apps-route ones
// stay real so a plain row's hide still posts to its own route.
const shareCalls = vi.hoisted(() => ({ hide: vi.fn(), unhide: vi.fn(), patch: vi.fn() }))
vi.mock('@/api/shares', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/shares')>()),
  useHideShare: () => ({ mutate: shareCalls.hide }),
  useUnhideShare: () => ({ mutate: shareCalls.unhide }),
  usePatchShare: () => ({ mutate: shareCalls.patch }),
}))

import AppsOverlay from '@/components/apps/AppsOverlay'
import { appChipClass, unpinWords } from '@/components/apps/appRow'
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

describe('AppsOverlay — a row placed here by a share (SHARING.md)', () => {
  const placement = {
    kind: 'agent' as const, share_id: 'sh-1', from_agent: 'ops', from_agent_name: 'Ops Desk',
    agent: 'dev', role_cap: 'editor' as const, shared_by: 'u-nora', shared_by_name: 'Nora', can_remove: false,
  }
  const placed = (over: Partial<PinnedApp> = {}) => mkApp('register', {
    agent: 'ops', can_manage: false, can_approve: false, viewer_role: 'viewer', placement, ...over,
  })

  it('the frame gets the row\'s own agent and no send_prompt; the chip carries the mark', () => {
    appsData.current = [mkApp('brief'), placed()]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    expect(frameProps.current.agent).toBe('ops')
    expect(frameProps.current.onSendPrompt).toBeUndefined()
    expect(screen.getByTestId('placed-mark')).toBeTruthy()
    expect(screen.getByTitle('register · from Ops Desk')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /^brief$/ }))
    expect(frameProps.current.agent).toBe('dev')
  })

  it('the menu is the reduced one: hide through the share, where it comes from, no management', () => {
    appsData.current = [mkApp('brief'), placed()]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    openMenu('register')
    expect(screen.queryByRole('menuitem', { name: 'Share' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: /Unpin/ })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'Remove from this agent' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'Remove for me' })).toBeNull()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Where it comes from' }))
    expect(screen.getByTestId('app-where-from').textContent).toContain('comes from Ops Desk, shared by Nora with this agent')
    openMenu('register')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Hide for me' }))
    expect(shareCalls.hide).toHaveBeenCalledWith({ id: 'sh-1', agent: 'dev' })
  })

  it('says which sessions may call a placed app\'s exported methods, from the share\'s kind and cap', () => {
    const exports = { methods: { status: { description: 's' }, 'update-project': { description: 'w', min_role: 'editor' as const } } }
    appsData.current = [mkApp('brief'), placed({ exports, viewer_role: 'manager' })]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    openMenu('register')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Where it comes from' }))
    expect(screen.getByTestId('app-where-from').textContent).toContain(
      'You use it as manager. Chats and tasks of this agent may call its 2 exported methods, up to editor. A task with no person calls only those without a role floor.')
    cleanup()
    appsData.current = [mkApp('brief'), placed({ exports, placement: { ...placement, kind: 'person', role_cap: 'viewer' } })]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    openMenu('register')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Where it comes from' }))
    expect(screen.getByTestId('app-where-from').textContent).toContain('with you. You use it as viewer. Your own chats and tasks here may call its 2 exported methods as viewer, while they run in your personal space (never on a Shared-only agent).')
    cleanup()
    appsData.current = [mkApp('brief'), placed()]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    openMenu('register')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Where it comes from' }))
    expect(screen.getByTestId('app-where-from').textContent).not.toContain('may call')
  })

  it('an editor of this agent removes it behind a confirm, which revokes the share', () => {
    appsData.current = [mkApp('brief'), placed({ placement: { ...placement, can_remove: true } })]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    openMenu('register')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove from this agent' }))
    expect(shareCalls.patch).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Remove from this agent' }))
    expect(shareCalls.patch).toHaveBeenCalledWith({ id: 'sh-1', revoke: true })
  })

  it('a hidden placed row restores through the share, and a placed row is never dragged', () => {
    appsData.current = [mkApp('brief'), mkApp('ops'), placed({ hidden_for_me: true })]
    const first = renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: '+1 hidden' }))
    fireEvent.click(screen.getByRole('button', { name: 'register ↺' }))
    expect(shareCalls.unhide).toHaveBeenCalledWith({ id: 'sh-1', agent: 'dev' })
    first.unmount()
    appsData.current = [mkApp('brief'), placed()]
    const { unmount } = renderOverlay()
    unmount()
    renderOverlay()
    expect(screen.getByRole('button', { name: /^brief$/ }).getAttribute('draggable')).toBe('true')
    expect(screen.getByRole('button', { name: /register/ }).getAttribute('draggable')).toBe('false')
  })

  it('a drop reorders the agent\'s own rows only; the placed row stays last in the cache and out of the payload', () => {
    reorderMutate.mockClear()
    appsData.current = [mkApp('brief'), mkApp('ops'), placed()]
    renderOverlay()
    fireEvent.dragStart(screen.getByRole('button', { name: /^brief$/ }))
    fireEvent.drop(screen.getByRole('button', { name: /^ops$/ }))
    expect(reorderMutate).toHaveBeenCalledWith(['id-ops', 'id-brief'])
    expect(reorderMutate.mock.calls[0][0]).not.toContain(placed().id)
    // A drop onto the placed row, or dragging it, moves nothing.
    reorderMutate.mockClear()
    fireEvent.dragStart(screen.getByRole('button', { name: /register/ }))
    fireEvent.drop(screen.getByRole('button', { name: /^brief$/ }))
    fireEvent.dragStart(screen.getByRole('button', { name: /^brief$/ }))
    fireEvent.drop(screen.getByRole('button', { name: /register/ }))
    expect(reorderMutate).not.toHaveBeenCalled()
  })

  it('a person\'s own placement offers Remove for me in place of hide, which revokes their share behind a confirm', () => {
    shareCalls.hide.mockClear()
    shareCalls.patch.mockClear()
    appsData.current = [mkApp('brief'), placed({ placement: { ...placement, kind: 'person', role_cap: 'viewer' } })]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    openMenu('register')
    expect(screen.queryByRole('menuitem', { name: 'Hide for me' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'Remove from this agent' })).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Where it comes from' })).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: 'Remove for me' }).getAttribute('title')).toBe(
      'Removes the share. The person who shared it can share it again.')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove for me' }))
    expect(shareCalls.patch).not.toHaveBeenCalled()
    expect(screen.getByTestId('app-remove-confirm').textContent).toBe(
      'Remove “register” from your apps? Nora can share it again.Remove for meCancel')
    fireEvent.click(screen.getByRole('button', { name: 'Remove for me' }))
    expect(shareCalls.patch).toHaveBeenCalledWith({ id: 'sh-1', revoke: true })
    expect(shareCalls.hide).not.toHaveBeenCalled()
    expect(screen.queryByTestId('app-remove-confirm')).toBeNull()
    // Without the sharer's name the sentence still reads whole.
    cleanup()
    appsData.current = [mkApp('brief'), placed({ placement: { ...placement, kind: 'person', shared_by_name: '' } })]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    openMenu('register')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove for me' }))
    expect(screen.getByTestId('app-remove-confirm').textContent).toContain(
      'from your apps? The person who shared it can share it again.')
  })

  it('the remove confirm acts on the share it was opened for: a new identity hides it, a tab switch cancels it', () => {
    shareCalls.patch.mockClear()
    const person = { ...placement, kind: 'person' as const, role_cap: 'viewer' as const }
    appsData.current = [mkApp('brief'), placed({ placement: person })]
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // A fresh element each time, so a rerender reads the list again.
    const tree = () => (
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/chat/dev']}>
          <AppsOverlay agent="dev" />
        </MemoryRouter>
      </QueryClientProvider>
    )
    const { rerender } = render(tree())
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    openMenu('register')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove for me' }))
    expect(screen.getByTestId('app-remove-confirm')).toBeTruthy()
    // A refetch: a team share made meanwhile now owns the row.
    appsData.current = [mkApp('brief'), placed({ placement: { ...placement, share_id: 'sh-2', can_remove: true } })]
    rerender(tree())
    expect(screen.queryByTestId('app-remove-confirm')).toBeNull()
    expect(shareCalls.patch).not.toHaveBeenCalled()
    // Back to the person's own row: a tab switch drops an armed confirm.
    appsData.current = [mkApp('brief'), placed({ placement: person })]
    rerender(tree())
    openMenu('register')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove for me' }))
    expect(screen.getByTestId('app-remove-confirm')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /^brief$/ }))
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    expect(screen.queryByTestId('app-remove-confirm')).toBeNull()
  })

  it('a hidden person placement restores through its share', () => {
    shareCalls.unhide.mockClear()
    appsData.current = [mkApp('brief'), placed({ hidden_for_me: true, placement: { ...placement, kind: 'person' } })]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: '+1 hidden' }))
    fireEvent.click(screen.getByRole('button', { name: 'register ↺' }))
    expect(shareCalls.unhide).toHaveBeenCalledWith({ id: 'sh-1', agent: 'dev' })
  })

  it('a department placement\'s remove names the whole department', () => {
    shareCalls.patch.mockClear()
    appsData.current = [mkApp('brief'), placed({ placement: { ...placement, kind: 'department', can_remove: true } })]
    renderOverlay()
    fireEvent.click(screen.getByRole('button', { name: /register/ }))
    openMenu('register')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove from this agent' }))
    expect(screen.getByTestId('app-remove-confirm').textContent).toContain('from every agent of the department')
    fireEvent.click(screen.getByRole('button', { name: 'Remove from the department' }))
    expect(shareCalls.patch).toHaveBeenCalledWith({ id: 'sh-1', revoke: true })
  })
})

describe('AppsOverlay — the chip of a row a share brought here', () => {
  const placement = {
    kind: 'agent' as const, share_id: 'sh-1', from_agent: 'ops', from_agent_name: 'Ops Desk',
    agent: 'dev', role_cap: 'viewer' as const, shared_by: 'u-nora', shared_by_name: 'Nora', can_remove: false,
  }
  const chip = (name: RegExp) => screen.getByRole('button', { name })
  const classes = (el: HTMLElement) => el.className.split(/\s+/)

  it('fills by whose it is and borders every shared-in row in teal, the home rows unchanged', () => {
    appsData.current = [
      mkApp('team'),
      mkApp('mine', { scope: 'personal' }),
      mkApp('person', { agent: 'ops', can_manage: false, placement: { ...placement, kind: 'person' } }),
      mkApp('board', { agent: 'ops', can_manage: false, placement }),
      mkApp('dept', { agent: 'ops', can_manage: false, placement: { ...placement, kind: 'department' } }),
      mkApp('colleague', { scope: 'personal', can_manage: false, granted: true }),
    ]
    renderOverlay()
    // The active home row (the first) is a solid purple chip with its own border.
    expect(classes(chip(/^team$/))).toEqual(expect.arrayContaining(['bg-p-accent-purple', 'border-p-accent-purple']))
    expect(classes(chip(/^mine$/))).toEqual(expect.arrayContaining(['bg-brand/10', 'border-brand/30']))
    expect(classes(chip(/^person$/))).toEqual(expect.arrayContaining(['bg-brand/10', 'text-brand', 'border-p-accent-teal']))
    expect(classes(chip(/^board$/))).toEqual(expect.arrayContaining(['bg-p-accent-purple/10', 'border-p-accent-teal']))
    expect(classes(chip(/^dept$/))).toEqual(expect.arrayContaining(['bg-p-accent-purple/10', 'border-p-accent-teal']))
    expect(classes(chip(/^colleague$/))).toEqual(expect.arrayContaining(['bg-brand/10', 'border-p-accent-teal']))
    expect(chip(/^team$/).className).not.toContain('teal')
    expect(chip(/^mine$/).className).not.toContain('teal')
    // The share mark on every shared-in row, never on a home row.
    expect(screen.getAllByTestId('placed-mark')).toHaveLength(4)
    expect(chip(/^team$/).querySelector('[data-testid="placed-mark"]')).toBeNull()
    // Active, a shared-in chip keeps its fill and adds the ring.
    fireEvent.click(chip(/^person$/))
    expect(classes(chip(/^person$/))).toEqual(expect.arrayContaining(['bg-brand', 'text-white', 'border-p-accent-teal', 'ring-2', 'ring-p-accent-teal']))
    fireEvent.click(chip(/^board$/))
    expect(classes(chip(/^board$/))).toEqual(expect.arrayContaining(['bg-p-accent-purple', 'text-white', 'border-p-accent-teal', 'ring-2']))
  })

  it('appChipClass: the fill follows ownership, the teal border follows the share', () => {
    const p = (kind: 'person' | 'agent' | 'department') => ({ scope: 'shared' as const, placement: { ...placement, kind } })
    expect(appChipClass({ scope: 'shared' }, false)).toBe('bg-p-accent-purple/10 text-p-accent-purple border-p-accent-purple/30 hover:bg-p-accent-purple/20')
    expect(appChipClass({ scope: 'personal' }, true)).toBe('bg-brand text-white border-brand')
    expect(appChipClass(p('person'), false)).toBe('bg-brand/10 text-brand border-p-accent-teal hover:bg-brand/20')
    expect(appChipClass(p('agent'), false)).toBe('bg-p-accent-purple/10 text-p-accent-purple border-p-accent-teal hover:bg-p-accent-purple/20')
    expect(appChipClass(p('department'), true)).toBe('bg-p-accent-purple text-white border-p-accent-teal ring-2 ring-p-accent-teal ring-offset-1 ring-offset-p-bg')
    expect(appChipClass({ scope: 'personal', granted: true }, true)).toBe('bg-brand text-white border-p-accent-teal ring-2 ring-p-accent-teal ring-offset-1 ring-offset-p-bg')
  })
})

describe('AppsOverlay — the soft unpin of an app that runs a server says Stop app', () => {
  const server = { kind: 'folder' as const, has_server: true }

  it('a shared server app reads Stop app for everyone, and the confirm says what is kept', () => {
    unpinMutate.mockClear()
    appsData.current = [mkApp('board', server)]
    renderOverlay()
    openMenu('board')
    expect(screen.queryByRole('menuitem', { name: /Unpin/ })).toBeNull()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Stop app for everyone' }))
    expect(unpinMutate).not.toHaveBeenCalled()
    expect(screen.getByTestId('app-unpin-confirm').textContent).toBe(
      'Stop “board” for everyone? Stops the app and keeps its data. Pin it again to start it.Stop app for everyoneCancel')
    fireEvent.click(screen.getByRole('button', { name: 'Stop app for everyone' }))
    expect(unpinMutate).toHaveBeenCalledWith('id-board')
  })

  it('a personal server app reads Stop app; a folder app with no live server and a single-file app unpin', () => {
    appsData.current = [mkApp('mine', { ...server, scope: 'personal' })]
    renderOverlay()
    openMenu('mine')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Stop app' }))
    expect(screen.getByTestId('app-unpin-confirm').textContent).toContain('Stop “mine”? Stops the app and keeps its data.')
    cleanup()
    appsData.current = [mkApp('static', { kind: 'folder', has_server: false })]
    renderOverlay()
    openMenu('static')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Unpin for everyone' }))
    expect(screen.getByTestId('app-unpin-confirm').textContent).toBe(
      'Unpin “static” for everyone? The workspace file and the approved actions are kept — ask the agent to pin it back anytime.Unpin for everyoneCancel')
    cleanup()
    appsData.current = [mkApp('page', { scope: 'personal', has_server: true })]
    renderOverlay()
    openMenu('page')
    expect(screen.getByRole('menuitem', { name: 'Unpin' })).toBeTruthy()
  })

  it('unpinWords: Stop only for a folder app whose live release runs a server', () => {
    const row = { title: 'Board', slug: 'board' }
    expect(unpinWords({ ...row, scope: 'shared', kind: 'folder', has_server: true }).action).toBe('Stop app for everyone')
    expect(unpinWords({ ...row, scope: 'personal', kind: 'folder', has_server: true }).question).toBe('Stop “Board”?')
    expect(unpinWords({ ...row, scope: 'shared', kind: 'folder', has_server: false }).action).toBe('Unpin for everyone')
    expect(unpinWords({ ...row, scope: 'shared', kind: 'folder' }).action).toBe('Unpin for everyone')
    expect(unpinWords({ ...row, scope: 'personal', kind: 'file', has_server: true }).action).toBe('Unpin')
  })
})
