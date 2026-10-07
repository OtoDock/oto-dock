import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import type { DeployStatus, PinnedApp } from '@/api/apps'

// The card reads the deploy status and posts the decision; both go to the
// proxy through apiFetch — stubbed here.
const calls = vi.hoisted(() => [] as { path: string; method: string; body?: string }[])
const statusStub = vi.hoisted(() => ({ current: null as DeployStatus | null }))
vi.mock('@/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/auth')>()),
  apiFetch: vi.fn(async (path: string, init?: RequestInit) => {
    calls.push({ path, method: init?.method || 'GET', body: typeof init?.body === 'string' ? init.body : undefined })
    if (path.endsWith('/deploy/status')) {
      return new Response(JSON.stringify(statusStub.current), { status: 200 })
    }
    return new Response('{}', { status: 200 })
  }),
}))

import AppDeployCard from '@/components/apps/AppDeployCard'
import AppMenu from '@/components/apps/AppMenu'
import { appNeedsApproval } from '@/components/apps/AppApprovalCard'

function mkApp(over: Partial<PinnedApp> = {}): PinnedApp {
  return {
    id: 'app-1', slug: 'kanban', title: 'Kanban', scope: 'shared', position: 0,
    rel_path: 'workspace/apps/kanban', updated_at: 't', kind: 'folder',
    actions: [{ id: 'me', label: 'Who am I', type: 'platform', method: 'viewer.me' }],
    actions_sig: 'sig', actions_approved: false, approval_stale: false,
    can_approve: true, can_manage: true, hidden_for_me: false,
    deploy_state: 'pending', pending_release: 2, release: 1, release_sha: 'abc',
    ...over,
  }
}

function renderIn(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>,
  )
}

describe('AppDeployCard', () => {
  it('names the pending release, what it changes and the manifest, and approves in one click', async () => {
    calls.length = 0
    statusStub.current = {
      deploy_state: 'pending', release: 1, pending_release: 2, manifest_approved: false,
      deploy_requires_approval: false, deploying: false, server: 'up', error: '', retry_after: 0,
      changes: { added: ['server/index.ts'], changed: ['client/index.html', 'app.json'], removed: [] },
    }
    renderIn(<AppDeployCard app={mkApp()} agent="dev" />)
    expect(screen.getByTestId('app-deploy-card').textContent).toContain('Release 2 of “Kanban” is waiting for approval')
    expect(screen.getByTestId('app-deploy-card').textContent).toContain('viewers keep release 1')
    // The plain list opens with the counts; the names and the groups sit
    // behind Details.
    await waitFor(() => expect(screen.getByTestId('deploy-changes').textContent).toBe('Adds 1 file and changes 2'))
    expect(screen.getByTestId('summary-data').textContent).toBe('Uses the platform for who you are')
    expect(screen.queryByTestId('deploy-files')).toBeNull()
    expect(screen.queryByText('Who am I')).toBeNull()
    fireEvent.click(screen.getByTestId('details-toggle'))
    expect(screen.getByText(/2 changed/).parentElement?.textContent).toContain('client/index.html')
    expect(screen.getByText('Who am I')).toBeTruthy()
    expect(screen.getByText(/What the app may do changed/)).toBeTruthy()
    fireEvent.click(screen.getByText('Approve and go live'))
    await waitFor(() => expect(calls.some((c) => c.path === '/v1/apps/app-1/deploy/approve' && c.method === 'POST')).toBe(true))
    // The click names what the card showed: a deploy that replaced the
    // waiting copy since is refused instead of approved unseen.
    const sent = calls.find((c) => c.path === '/v1/apps/app-1/deploy/approve')
    expect(JSON.parse(sent?.body || '{}')).toEqual({ release: 2, sig: 'sig' })
  })

  it('rejects, and says who decides when the viewer may not', async () => {
    calls.length = 0
    statusStub.current = {
      deploy_state: 'pending', release: 0, pending_release: 1, manifest_approved: true,
      deploy_requires_approval: true, deploying: false, server: 'stopped', error: '', retry_after: 0,
      changes: { added: ['app.json'], changed: [], removed: [] },
    }
    renderIn(<AppDeployCard app={mkApp({ actions_approved: true, release: 0, pending_release: 1 })} agent="dev" />)
    expect(screen.getByTestId('app-deploy-card').textContent).toContain('nothing serves until then')
    await waitFor(() => expect(screen.getByText(/Deploys of this app always wait/)).toBeTruthy())
    fireEvent.click(screen.getByText('Reject'))
    await waitFor(() => expect(calls.some((c) => c.path === '/v1/apps/app-1/deploy/reject')).toBe(true))
    const { container } = renderIn(<AppDeployCard app={mkApp({ can_manage: false })} agent="dev" />)
    expect(container.textContent).toContain('An editor of this agent decides.')
    expect(container.querySelector('button')?.textContent).not.toContain('Approve')
  })

  it('says that approving also turns approval off when the release lowers the switch', async () => {
    statusStub.current = {
      deploy_state: 'pending', release: 1, pending_release: 2, manifest_approved: true,
      deploy_requires_approval: true, deploying: false, server: 'up', error: '', retry_after: 0,
      changes: { added: [], changed: ['app.json'], removed: [] }, lowers_approval: true,
    }
    renderIn(<AppDeployCard app={mkApp({ actions_approved: true })} agent="dev" />)
    await waitFor(() => expect(screen.getByTestId('deploy-lowers-approval').textContent)
      .toContain('Approving this release also turns off approval'))
    expect(screen.queryByText(/always wait for a person/)).toBeNull()
    // Without the flag (or on an older proxy) the card reads as before.
    statusStub.current = { ...statusStub.current, lowers_approval: false }
    const { container } = renderIn(<AppDeployCard app={mkApp({ actions_approved: true })} agent="dev" />)
    await waitFor(() => expect(container.textContent).toContain('always wait for a person'))
    expect(container.querySelector('[data-testid="deploy-lowers-approval"]')).toBeNull()
  })

  it('takes the approval card\'s slot while a release is pending', () => {
    expect(appNeedsApproval(mkApp())).toBe(false)
    expect(appNeedsApproval(mkApp({ deploy_state: 'idle' }))).toBe(true)
  })

  // APPS.md "Secrets": a required secret without a value parks the release
  // even when the manifest is approved — the card says which, keeps Approve
  // off, lists the secrets with their set flags and opens the settings.
  it('waits for a secret: says which, keeps Approve off and offers the settings', async () => {
    calls.length = 0
    statusStub.current = {
      deploy_state: 'pending', release: 0, pending_release: 1, manifest_approved: true,
      deploy_requires_approval: false, deploying: false, server: 'stopped', error: '', retry_after: 0,
      changes: { added: ['app.json'], changed: [], removed: [] },
      waiting: 'STRIPE_SECRET_KEY is not set',
      secrets: [
        { name: 'STRIPE_SECRET_KEY', required: true, set: false, declared: true,
          sends_to: { host: 'api.example.test', header: 'Authorization' } },
        { name: 'SMTP_PASSWORD', required: false, set: true, declared: true, env: true },
      ],
    }
    renderIn(<AppDeployCard app={mkApp({ actions_approved: true, release: 0, pending_release: 1, egress: ['api.example.test'] })} agent="dev" />)
    await waitFor(() => expect(screen.getByTestId('deploy-waiting')).toBeTruthy())
    expect(screen.getByTestId('deploy-waiting').textContent).toContain('Waiting for a secret: STRIPE_SECRET_KEY is not set')
    expect(screen.getByText(/The manifest is approved\. A secret it needs has no value yet\./)).toBeTruthy()
    const approve = screen.getByText('Approve and go live') as HTMLButtonElement
    expect(approve.disabled).toBe(true)
    // An approved manifest is not said again: the secrets line alone, then
    // the Secrets group behind Details.
    expect(screen.getByTestId('summary-secrets').textContent).toBe('Needs the secrets STRIPE_SECRET_KEY (not set) and SMTP_PASSWORD (set)Set')
    expect(screen.queryByTestId('summary-data')).toBeNull()
    fireEvent.click(screen.getByTestId('details-toggle'))
    const lines = screen.getAllByTestId('secret-line').map((n) => n.textContent || '')
    expect(lines[0]).toContain('needs the secret STRIPE_SECRET_KEY')
    expect(lines[0]).toContain('not set — required')
    expect(lines[1]).toContain('SMTP_PASSWORD')
    expect(screen.queryByTestId('manifest-blocks')).toBeNull()
    fireEvent.click(screen.getByTestId('deploy-set-secrets'))
    expect(screen.getByTestId('app-settings-panel')).toBeTruthy()
    expect(calls.some((c) => c.path === '/v1/apps/app-1/secrets')).toBe(true)
  })

  it('uses the approval card\'s shell: pinned header and footer, a scrolling body', async () => {
    statusStub.current = {
      deploy_state: 'pending', release: 1, pending_release: 2, manifest_approved: false,
      deploy_requires_approval: false, deploying: false, server: 'up', error: '', retry_after: 0,
      changes: { added: [], changed: ['app.json'], removed: [] },
    }
    const many = Array.from({ length: 7 }, (_, i) => ({
      id: `b${i}`, label: `Button ${i}`, type: 'send_prompt' as const, prompt: `p${i}`,
    }))
    renderIn(<AppDeployCard app={mkApp({ actions: many })} agent="dev" />)
    const card = screen.getByTestId('app-deploy-card')
    expect(card.className).toContain('min-h-0')
    expect(card.className).toContain('flex-col')
    expect(screen.getByTestId('app-card-body').className).toContain('overflow-y-auto')
    await waitFor(() => expect(screen.getByTestId('deploy-changes').textContent).toBe('Changes 1'))
    expect(screen.getByTestId('summary-buttons').textContent).toBe('7 buttons: Button 0, Button 1, Button 2, Button 3 and 3 more')
    const footer = screen.getByTestId('app-card-footer')
    expect(footer.contains(screen.getByText('Approve and go live'))).toBe(true)
    expect(footer.contains(screen.getByText('Reject'))).toBe(true)
    expect(footer.contains(screen.getByTestId('details-toggle'))).toBe(true)
    expect(footer.contains(screen.getByTestId('exact-manifest-toggle'))).toBe(true)
    fireEvent.click(screen.getByTestId('details-toggle'))
    expect(screen.getByTestId('app-card-body').contains(screen.getByTestId('manifest-blocks'))).toBe(true)
    expect(screen.getByText(/1 changed/).parentElement?.textContent).toContain('app.json')
  })
})

describe('AppMenu — folder entries', () => {
  const open = () => fireEvent.click(screen.getByLabelText('Options for Kanban'))

  it('offers Logs, the working-copy toggle and Delete to whoever may manage a folder app', () => {
    const onLogs = vi.fn()
    const onTogglePreview = vi.fn()
    const onDelete = vi.fn()
    const onSettings = vi.fn()
    renderIn(<AppMenu app={mkApp({ preview_sha: 'pre' })} onLogs={onLogs} onSettings={onSettings} onTogglePreview={onTogglePreview} onDelete={onDelete} />)
    open()
    fireEvent.click(screen.getByText('Logs'))
    expect(onLogs).toHaveBeenCalled()
    open()
    fireEvent.click(screen.getByText('Settings'))
    expect(onSettings).toHaveBeenCalled()
    open()
    fireEvent.click(screen.getByText('View the working copy'))
    expect(onTogglePreview).toHaveBeenCalled()
    open()
    fireEvent.click(screen.getByText('Delete app and its data'))
    expect(onDelete).toHaveBeenCalled()
  })

  it('portals the panel out of its own subtree, where the chip strip\'s scroller cannot clip it', () => {
    const { container } = renderIn(<AppMenu app={mkApp()} onChip onLogs={() => {}} />)
    open()
    const panel = screen.getByRole('menu') as HTMLElement
    expect(container.contains(panel)).toBe(false)
    expect(panel.parentElement).toBe(document.body)
    expect(panel.style.position).toBe('fixed')
  })

  it('shows the toggle\'s other side while previewing and nothing of it without a preview copy', () => {
    renderIn(<AppMenu app={mkApp({ preview_sha: 'pre' })} onTogglePreview={() => {}} previewing />)
    open()
    expect(screen.getByText('View the live release')).toBeTruthy()
    const { queryByText } = renderIn(<AppMenu app={mkApp({ id: 'app-2', title: 'Plain', kind: 'file' })} onLogs={() => {}} onTogglePreview={() => {}} onDelete={() => {}} />)
    fireEvent.click(screen.getByLabelText('Options for Plain'))
    expect(queryByText('Logs')).toBeNull()
    expect(queryByText('Settings')).toBeNull()
    expect(queryByText('Delete app and its data')).toBeNull()
  })
})
