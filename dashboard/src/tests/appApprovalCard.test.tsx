import { describe, it, expect } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { type PinnedApp } from '@/api/apps'
import { meetsFloor } from '@/lib/permissions'
import AppApprovalCard, { appNeedsApproval } from '@/components/apps/AppApprovalCard'

// The per-action role floor (APPS.md "min_role"): the card names it in
// words, and the host judges a feed's floor the way the server judges a
// button's.

function mkApp(over: Partial<PinnedApp> = {}): PinnedApp {
  return {
    id: 'id-a', slug: 'a', title: 'Ops', scope: 'shared', position: 0,
    rel_path: 'workspace/apps/a.html', updated_at: '', actions: [],
    actions_sig: 'sig', actions_approved: false, approval_stale: false,
    can_approve: true, can_manage: true, hidden_for_me: false, ...over,
  }
}

function renderCard(app: PinnedApp) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AppApprovalCard app={app} agent="dev" />
    </QueryClientProvider>,
  )
}

/** The groups with every sentence sit behind the "Details" fold. */
function openDetails() {
  fireEvent.click(screen.getByTestId('details-toggle'))
}

const summaryText = () => screen.getByTestId('manifest-summary').textContent || ''

describe('AppApprovalCard — role floors', () => {
  it('names an action floor in words and says nothing for the default', () => {
    renderCard(mkApp({ actions: [
      { id: 'restart', label: 'Restart', type: 'send_prompt', prompt: 'restart it', min_role: 'editor' },
      { id: 'wipe', label: 'Wipe', type: 'send_prompt', prompt: 'wipe it', min_role: 'manager' },
      { id: 'stats', label: 'Stats', type: 'send_prompt', prompt: 'stats' },
    ] }))
    openDetails()
    expect(screen.getByText('editors and up')).toBeTruthy()
    expect(screen.getByText('managers only')).toBeTruthy()
    expect(screen.getAllByText(/editors and up|managers only/)).toHaveLength(2)
  })

  it('names the contributor floor: the workspace tier, below editors', () => {
    renderCard(mkApp({ actions: [
      { id: 'log', label: 'Log the day', type: 'send_prompt', prompt: 'log it', min_role: 'contributor' },
    ] }))
    openDetails()
    expect(screen.getByText('contributors and up')).toBeTruthy()
  })
})

// The plain list (found on the phone 2026-09-18: the groups and two folds
// stacked left nothing readable): one short line per kind of thing the
// app may do, always open; the sentences behind "Details"; the JSON
// behind "Exact manifest"; both toggles in the pinned footer.
describe('AppApprovalCard — the plain list', () => {
  const wide = () => mkApp({
    manifest_empty: false, kind: 'folder',
    actions: [
      { id: 'tell', label: 'Tell the team', type: 'platform', method: 'notifications.create' },
      { id: 'tasks', label: 'Scheduled work', type: 'data_feed', feed: 'tasks' },
      { id: 'me', label: 'Me', type: 'platform', method: 'viewer.me' },
    ],
    files: { read: ['workspace/team-pulse/'], write: ['workspace/team-pulse/'] },
    egress: ['api.stripe.com'],
    external: { links: ['checkout.stripe.com'], challenge: ['/register'], session_days: 7 },
    handlers: {
      on_schedule: { snapshot: { cron: '*/30 * * * *' } },
      on_event: { 'record-snapshot': ['step:snapshot'], 'note-style': ['check:note-style'] },
    },
    steps: { snapshot: { run: 'scripts/snapshot.sh', timeout: 120 } },
    inbound: { stripe: { verify: 'stripe', secret: 'STRIPE_WEBHOOK_SECRET', handler: 'payment' } },
    secrets: [
      { name: 'STRIPE_SECRET_KEY', required: true, declared: true, set: false,
        sends_to: { host: 'api.stripe.com', header: 'Authorization', prefix: 'Bearer ' } },
      { name: 'STRIPE_WEBHOOK_SECRET', required: true, declared: true, set: true },
    ],
    exports: { snapshots: { board: { description: 'the board as JSON' } } },
    bindings: [{ name: 'crm', agent: 'sales', app: 'leads' }],
    requires_status: {
      mcps: [{ name: 'github-mcp', assigned: true }],
      providers: [{ provider: 'github', mcp: 'github-mcp', connected: true, account: 'dimitris', identity: 'the agent' }],
    },
  })

  it('says one short line per kind, in order, and keeps the sentences behind Details', () => {
    renderCard(wide())
    const lines = Array.from(screen.getByTestId('manifest-summary').querySelectorAll('li')).map((n) => n.textContent || '')
    expect(lines).toEqual([
      'Uses the platform for sending notifications, the live tasks feed and who you are',
      'Reads files under workspace/team-pulse/',
      'Writes files under workspace/team-pulse/',
      'Its server may reach api.stripe.com',
      'Its page may open checkout.stripe.com',
      'Bot check before /register',
      'Keeps a visitor’s login for 7 days',
      'Wakes every 30 minutes, when the step snapshot finishes, when the check note-style asks it to judge a turn and on Stripe-signed events',
      'Runs the script scripts/snapshot.sh where this agent runs',
      'Its scripts receive the github account of the agent — a manager must approve',
      'Needs the secrets STRIPE_SECRET_KEY (not set) and STRIPE_WEBHOOK_SECRET (set)Set',
      'Offers other apps the snapshot board',
      'Uses the app leads of agent sales',
      'Needs the MCP github-mcp (assigned)',
      'Needs a github account — the agent’s',
    ])
    // Nothing of the groups until the fold opens; the toggles sit in the footer.
    expect(screen.queryByTestId('manifest-blocks')).toBeNull()
    expect(screen.queryByTestId('exact-manifest')).toBeNull()
    const footer = screen.getByTestId('app-card-footer')
    expect(footer.contains(screen.getByTestId('details-toggle'))).toBe(true)
    expect(footer.contains(screen.getByTestId('exact-manifest-toggle'))).toBe(true)
    expect(footer.contains(screen.getByText('Approve'))).toBe(true)
    openDetails()
    const body = screen.getByTestId('app-card-body')
    expect(body.contains(screen.getByTestId('manifest-blocks'))).toBe(true)
    expect(screen.getByTestId('details-toggle').textContent).toBe('Hide details')
    expect(body.textContent).toContain('wakes on the schedule */30 * * * * as snapshot')
    fireEvent.click(screen.getByTestId('exact-manifest-toggle'))
    expect(body.contains(screen.getByTestId('exact-manifest'))).toBe(true)
    expect(screen.getByTestId('exact-manifest-toggle').textContent).toBe('Hide manifest')
  })

  it('counts the buttons and names a few, warns in amber, and offers one Set button', () => {
    const many = Array.from({ length: 7 }, (_, i) => ({
      id: `b${i}`, label: `Button ${i}`, type: 'fire_task' as const, task_id: `t${i}`, task_name: `Task ${i}`,
    }))
    renderCard(mkApp({
      manifest_empty: false, kind: 'folder', actions: many,
      handlers: { on_trigger: ['github'] },
      secrets: [{ name: 'API_KEY', required: false, declared: true, set: false }],
      requires_status: { mcps: [{ name: 'slack-mcp', assigned: false }], providers: [] },
    }))
    expect(screen.getByTestId('summary-buttons').textContent).toBe('7 buttons: Button 0, Button 1, Button 2, Button 3 and 3 more')
    expect(screen.getByTestId('summary-wakes').textContent).toBe('Wakes on the trigger github')
    const warn = screen.getByText('Its wakes may press its buttons unattended')
    expect(warn.parentElement?.className).toContain('text-amber-600')
    expect(screen.getByTestId('summary-secrets').textContent).toBe('Needs the secret API_KEY (optional, not set)Set')
    expect(screen.getByTestId('summary-secrets').className).not.toContain('text-amber-600')
    const mcp = screen.getByTestId('summary-mcp-slack-mcp')
    expect(mcp.textContent).toBe('Needs the MCP slack-mcp — not assigned to this agent')
    expect(mcp.className).toContain('text-amber-600')
    fireEvent.click(screen.getByTestId('summary-secret-set'))
    expect(screen.getByTestId('app-settings-panel')).toBeTruthy()
  })

  it('offers no Set button to a viewer who cannot manage the app', () => {
    renderCard(mkApp({
      manifest_empty: false, kind: 'folder', can_manage: false,
      secrets: [{ name: 'API_KEY', required: true, declared: true, set: false }],
    }))
    expect(screen.getByTestId('summary-secrets').textContent).toBe('Needs the secret API_KEY (not set)')
    expect(screen.queryByTestId('summary-secret-set')).toBeNull()
  })
})

// The card is driven by the manifest's blocks (APPS.md "The approval
// card"): a manifest with no buttons still needs approval when it declares
// files, hosts, wakes, exports, bindings or needs, and each block is said
// in words behind the Details fold.
describe('AppApprovalCard — every block in words', () => {
  it('needs approval for a manifest with blocks and no buttons, and says each block', () => {
    const app = mkApp({
      manifest_empty: false,
      files: { read: ['workspace/notes'], write: [] },
      egress: ['api.example.com'],
      handlers: {
        on_schedule: { digest: { cron: '0 7 * * 1-5' } },
        on_trigger: ['github'],
        on_event: { refresh: ['task_finished', 'app:crm:lead-created'] },
      },
      exports: { snapshots: { board: { description: 'the board as JSON', min_role: 'editor' } } },
      bindings: [{ name: 'crm', agent: 'sales', app: 'leads' }],
      requires: { mcps: ['github-mcp'], providers: ['github'] },
      requires_status: {
        mcps: [{ name: 'github-mcp', assigned: true }],
        providers: [{ provider: 'github', mcp: 'github-mcp', connected: false }],
      },
    })
    expect(appNeedsApproval(app)).toBe(true)
    expect(appNeedsApproval(mkApp({ manifest_empty: true }))).toBe(false)
    expect(appNeedsApproval(mkApp())).toBe(false)   // an older proxy: the actions decide
    renderCard(app)
    expect(document.body.textContent).toContain('declares what it may do')
    expect(summaryText()).toContain('Wakes on the schedule 0 7 * * 1-5, on the trigger github, when a task finishes and when the app behind crm emits lead-created')
    expect(summaryText()).toContain('Needs a github account — not connected for you')
    openDetails()
    const text = screen.getByTestId('manifest-blocks').textContent || ''
    expect(text).toContain('reads files under workspace/notes')
    expect(text).toContain('may reach api.example.com')
    // The carve opens an address: the line says what else answers there.
    expect(text).toContain('other sites that share their address')
    expect(text).toContain('never your local network or the platform')
    expect(text).not.toContain('and nothing else')
    expect(text).toContain('wakes on the schedule 0 7 * * 1-5 as digest')
    expect(text).toContain('a trigger aimed at github')
    expect(text).toContain('a task finishes or the app behind crm emits lead-created')
    expect(text).toContain('publishes the snapshot board — the board as JSON')
    expect(text).toContain('calls the app leads of agent sales as crm')
    expect(text).toContain('assigned to this agent')
    expect(text).toContain('not connected for you')
    expect(screen.queryByText(/press the app’s buttons unattended/)).toBeNull()
  })

  it('names whose account a provider need runs with', () => {
    // A shared app's buttons run with the agent's service account: the
    // card says so, or says whose job the binding is — never "for you".
    renderCard(mkApp({
      manifest_empty: false,
      requires: { providers: ['github', 'google'] },
      requires_status: {
        mcps: [],
        providers: [
          { provider: 'github', mcp: 'github-mcp', connected: true, account: 'dimitris', identity: 'the agent' },
          { provider: 'google', mcp: 'google-workspace', connected: false, account: '', identity: 'the agent' },
        ],
      },
    }))
    expect(summaryText()).toContain('Needs a github account — the agent’s')
    expect(summaryText()).toContain('Needs a google account — none bound to this agent')
    openDetails()
    const text = screen.getByTestId('manifest-blocks').textContent || ''
    expect(text).toContain("the agent's account dimitris")
    expect(text).toContain('no account is bound to this agent (Agent Settings → MCPs')
    expect(text).not.toContain('not connected for you')
    document.body.innerHTML = ''
    renderCard(mkApp({
      manifest_empty: false, scope: 'personal',
      requires: { providers: ['google'] },
      requires_status: { mcps: [], providers: [
        { provider: 'google', mcp: 'google-workspace', connected: true, account: 'me@example.com', identity: 'you' },
      ] },
    }))
    expect(summaryText()).toContain('Needs a google account — yours')
    openDetails()
    expect(document.body.textContent).toContain('connected as me@example.com')
  })

  it('warns when wakes and buttons meet, and keeps the buttons-only headline otherwise', () => {
    renderCard(mkApp({
      manifest_empty: false,
      actions: [{ id: 'r', label: 'Refresh', type: 'fire_task', task_id: 't1', task_name: 'Refresh stats' }],
      handlers: { on_trigger: ['github'] },
    }))
    expect(screen.getByText('Its wakes may press its buttons unattended')).toBeTruthy()
    openDetails()
    expect(screen.getByText(/wakes may press the app’s buttons unattended/)).toBeTruthy()
    expect(document.body.textContent).toContain('declares what it may do')
    document.body.innerHTML = ''
    renderCard(mkApp({ actions: [{ id: 's', label: 'Stats', type: 'send_prompt', prompt: 'stats' }] }))
    expect(document.body.textContent).toContain('declares 1 action button — review before they work')
    expect(screen.getByTestId('summary-buttons').textContent).toBe('1 button: Stats')
  })
})

// Secrets (APPS.md "Secrets"): each declared name is said with where its
// value goes and whether a person has set one; the exact manifest shows
// the declaration and never a flag or a value; a Set button opens the
// settings panel for whoever may manage a folder app.
describe('AppApprovalCard — secrets', () => {
  const secretive = (over: Partial<PinnedApp> = {}) => mkApp({
    manifest_empty: false, kind: 'folder',
    egress: ['api.example.test'],
    secrets: [
      { name: 'STRIPE_SECRET_KEY', required: true, description: 'a restricted key', declared: true, set: false,
        sends_to: { host: 'api.example.test', header: 'Authorization', prefix: 'Bearer ' } },
      { name: 'STRIPE_WEBHOOK_SECRET', required: true, declared: true, set: true, set_by: 'alice' },
      { name: 'SMTP_PASSWORD', required: false, declared: true, set: false, env: true },
      { name: 'OLD_KEY', required: false, declared: false, set: true },
    ],
    ...over,
  })

  it('says each secret, its use and whether it is set, and keeps values and flags out of the exact manifest', () => {
    renderCard(secretive())
    expect(screen.getByTestId('summary-secrets').textContent)
      .toBe('Needs the secrets STRIPE_SECRET_KEY (not set), STRIPE_WEBHOOK_SECRET (set) and SMTP_PASSWORD (optional, not set)Set')
    expect(screen.getByTestId('summary-secrets').className).toContain('text-amber-600')
    openDetails()
    const lines = screen.getAllByTestId('secret-line').map((n) => n.textContent || '')
    expect(lines).toHaveLength(3)
    expect(lines[0]).toContain('needs the secret STRIPE_SECRET_KEY (a restricted key)')
    expect(lines[0]).toContain('sent by the platform to api.example.test as Authorization, never seen by the server')
    expect(lines[0]).toContain('not set — required before the release goes live')
    expect(lines[1]).toContain('set')
    expect(lines[1]).not.toContain('not set')
    expect(lines[2]).toContain('read by the server itself')
    expect(lines[2]).toContain('not set (optional)')
    expect(screen.getByTestId('secret-set-STRIPE_SECRET_KEY').textContent).toBe('Set')
    expect(screen.getByTestId('secret-set-STRIPE_WEBHOOK_SECRET').textContent).toBe('Change')
    fireEvent.click(screen.getByTestId('exact-manifest-toggle'))
    const exact = JSON.parse(screen.getByTestId('exact-manifest').textContent || '{}')
    expect(exact.secrets).toEqual([
      { name: 'STRIPE_SECRET_KEY', required: true, description: 'a restricted key',
        sends_to: { host: 'api.example.test', header: 'Authorization', prefix: 'Bearer ' } },
      { name: 'STRIPE_WEBHOOK_SECRET', required: true },
      { name: 'SMTP_PASSWORD', required: false, env: true },
    ])
    expect(JSON.stringify(exact)).not.toContain('OLD_KEY')
    expect(JSON.stringify(exact)).not.toContain('"set"')
  })

  it('offers no Set button to a viewer who cannot manage the app, and opens the panel for one who can', () => {
    renderCard(secretive({ can_manage: false }))
    openDetails()
    expect(screen.queryByTestId('secret-set-STRIPE_SECRET_KEY')).toBeNull()
    document.body.innerHTML = ''
    renderCard(secretive())
    openDetails()
    fireEvent.click(screen.getByTestId('secret-set-STRIPE_SECRET_KEY'))
    expect(screen.getByTestId('app-settings-panel')).toBeTruthy()
    expect(screen.getByRole('dialog').getAttribute('aria-label')).toBe('Settings of Ops')
  })
})

// What a link may do (APPS.md "External links"): the hosts the page may
// open sit under Hosts even without a server host; the bot-check paths
// and a non-default login lifetime make an "On a link" group; the exact
// manifest carries the declaration, not the defaults.
describe('AppApprovalCard — what a link may do', () => {
  it('says the hosts the page may open, the bot-check paths and the login lifetime', () => {
    renderCard(mkApp({
      manifest_empty: false, kind: 'folder',
      external: { links: ['checkout.stripe.com'], challenge: ['/register'], session_days: 7 },
    }))
    openDetails()
    expect(screen.getByTestId('external-links-line').textContent).toContain('its page may open checkout.stripe.com in a new tab')
    expect(screen.getByTestId('external-challenge-line').textContent).toContain('pass a bot check before /register')
    expect(screen.getByTestId('external-session-line').textContent).toContain('login for 7 days')
    expect(document.body.textContent).toContain('On a link')
    fireEvent.click(screen.getByTestId('exact-manifest-toggle'))
    const exact = JSON.parse(screen.getByTestId('exact-manifest').textContent || '{}')
    expect(exact.external).toEqual({ links: ['checkout.stripe.com'], challenge: ['/register'], session_days: 7 })
    document.body.innerHTML = ''
    renderCard(mkApp({ manifest_empty: false, kind: 'folder', external: { links: [], challenge: [], session_days: 30 } }))
    openDetails()
    expect(screen.queryByTestId('external-links-line')).toBeNull()
    expect(document.body.textContent).not.toContain('On a link')
  })
})

// Inbound hooks (APPS.md "Inbound hooks"): each hook is a Wakes line with
// the vendor's scheme, the address, the handler and the secret, plus the
// server-only note; an inbound wake alone never makes the "may press the
// buttons unattended" line, since it never may.
describe('AppApprovalCard — inbound hooks', () => {
  it('says each hook with its scheme, address, handler and secret, and the server-only note', () => {
    renderCard(mkApp({
      manifest_empty: false, kind: 'folder',
      actions: [{ id: 'r', label: 'Refresh', type: 'fire_task', task_id: 't1', task_name: 'Refresh' }],
      inbound: {
        stripe: { verify: 'stripe', secret: 'STRIPE_WEBHOOK_SECRET', handler: 'payment' },
        gh: { verify: 'github', secret: 'GH_SECRET', handler: 'push' },
        generic: { verify: 'hmac_sha256', secret: 'HMAC_SECRET', handler: 'generic', header: 'X-Signature' },
        token: { verify: 'bearer', secret: 'BEARER_SECRET', handler: 'poke' },
      },
    }))
    expect(screen.getByTestId('summary-wakes').textContent)
      .toBe('Wakes on Stripe-signed events, on GitHub-signed events, on HMAC-signed events (the X-Signature header) and on bearer-authenticated events')
    expect(screen.queryByText(/press its buttons unattended/)).toBeNull()
    openDetails()
    const lines = screen.getAllByTestId('inbound-line').map((n) => n.textContent || '')
    expect(lines).toHaveLength(4)
    expect(lines[0]).toContain('receives Stripe-signed events at /v1/apps/id-a/inbound/stripe as payment, verified with the secret STRIPE_WEBHOOK_SECRET')
    expect(lines[1]).toContain('GitHub-signed events')
    expect(lines[2]).toContain('HMAC-signed events (the X-Signature header)')
    expect(lines[3]).toContain('bearer-authenticated events')
    expect(screen.getByTestId('inbound-note').textContent).toContain('never files, buttons or a chat')
    expect(document.body.textContent).toContain('Wakes (4)')
    expect(screen.queryByText(/press the app’s buttons unattended/)).toBeNull()
    fireEvent.click(screen.getByTestId('exact-manifest-toggle'))
    const exact = JSON.parse(screen.getByTestId('exact-manifest').textContent || '{}')
    expect(exact.inbound.stripe).toEqual({ verify: 'stripe', secret: 'STRIPE_WEBHOOK_SECRET', handler: 'payment' })
  })
})

// Steps (APPS.md "Steps"): a handler that runs a script where the agent
// runs is said with its file, its hash, its bound and its place; the
// scripts' accounts and the manager-only approval of a shared app; the
// "link input is data" line when a live link reaches the app; a step's
// finish is an event a server handler may wake on.
describe('AppApprovalCard — steps', () => {
  const stepped = (over: Partial<PinnedApp> = {}) => mkApp({
    manifest_empty: false,
    handlers: { on_schedule: { 'docs-sync': { cron: '0 * * * *' } }, on_event: { 'docs-synced': ['step:docs-sync'] } },
    steps: { 'docs-sync': { run: 'scripts/sync-docs.sh', timeout: 300, sha256: '3f9a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4' } },
    ...over,
  })

  it('says the script, its hash, its bound and where it runs, and counts the lines', () => {
    renderCard(stepped({ step_target: { kind: 'machine', name: 'OtoDock-dev-server' } }))
    expect(screen.getByTestId('summary-wakes').textContent).toBe('Wakes every hour and when the step docs-sync finishes')
    expect(screen.getByTestId('summary-steps').textContent).toBe('Runs the script scripts/sync-docs.sh on OtoDock-dev-server')
    openDetails()
    const text = document.body.textContent || ''
    expect(text).toContain('runs the script scripts/sync-docs.sh on OtoDock-dev-server when docs-sync wakes')
    expect(text).toContain('up to 5 minutes, sha256 3f9a1b2c3d4e')
    expect(text).toContain('wakes when the step docs-sync finishes (docs-synced)')
    expect(screen.queryByTestId('step-link-warning')).toBeNull()
    expect(screen.queryByTestId('step-accounts')).toBeNull()
    // The exact manifest carries the block with the full hash.
    fireEvent.click(screen.getByTestId('exact-manifest-toggle'))
    expect(screen.getByTestId('exact-manifest').textContent).toContain('3f9a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4')
    document.body.innerHTML = ''
    renderCard(stepped({ step_target: { kind: 'local' } }))
    expect(screen.getByTestId('summary-steps').textContent).toBe('Runs the script scripts/sync-docs.sh in this agent’s sandbox')
    openDetails()
    expect(document.body.textContent).toContain('in this agent’s sandbox when docs-sync wakes')
  })

  it('names the accounts the scripts receive, the manager-only approval and the link warning', () => {
    renderCard(stepped({
      requires: { providers: ['github'] },
      requires_status: { mcps: [], providers: [
        { provider: 'github', mcp: 'github-mcp', connected: true, account: 'dimitris', identity: 'the agent' },
      ] },
      has_live_link: true, steps_need_manager: true, can_approve: false,
    }))
    expect(screen.getByTestId('summary-step-accounts').textContent).toBe('Its scripts receive the github account of the agent — a manager must approve')
    expect(screen.getByTestId('summary-step-accounts').className).toContain('text-amber-600')
    expect(screen.getByTestId('summary-step-link').textContent).toContain('reachable by a link')
    openDetails()
    const text = document.body.textContent || ''
    expect(screen.getByTestId('step-accounts').textContent).toContain('the scripts receive the github account of the agent')
    expect(text).toContain('a manager of this agent must approve this')
    expect(screen.getByTestId('step-link-warning').textContent).toContain('reachable by a link')
    expect(text).toContain('Approval needs a manager of this agent: its scripts receive the agent’s accounts.')
    document.body.innerHTML = ''
    // A personal app's scripts get the owner's own account: no manager line.
    renderCard(stepped({
      scope: 'personal', requires: { providers: ['github'] },
      requires_status: { mcps: [], providers: [
        { provider: 'github', mcp: 'github-mcp', connected: true, account: 'me', identity: 'you' },
      ] },
    }))
    expect(screen.getByTestId('summary-step-accounts').textContent).toBe('Its scripts receive the github account of the owner')
    expect(screen.getByTestId('summary-step-accounts').className).not.toContain('text-amber-600')
    openDetails()
    expect(screen.getByTestId('step-accounts').textContent).toContain('of the owner')
    expect(document.body.textContent).not.toContain('a manager of this agent must approve this')
  })
})

// Checks (CHECKS.md): a button that fires a task names the checks it
// attaches to the run; a handler may be a check's handler section, and
// may wake on a turn's end or a verdict.
describe('AppApprovalCard — checks', () => {
  it('names the checks a fire_task button attaches, and the check events a handler wakes on', () => {
    renderCard(mkApp({
      manifest_empty: false,
      actions: [{ id: 'r', label: 'Release', type: 'fire_task', task_id: 't1', task_name: 'Release', checks: ['coding', 'style'] }],
      handlers: { on_event: { lint: ['check:coding'], tally: ['turn_finished', 'check_verdict'] } },
    }))
    expect(screen.getByTestId('summary-wakes').textContent)
      .toBe('Wakes when the check coding asks it to judge a turn, when a chat turn ends and when a check gives a verdict')
    openDetails()
    const text = document.body.textContent || ''
    expect(text).toContain('runs the task “Release”')
    expect(text).toContain('and the checks coding, style on its result')
    expect(text).toContain('the check coding asks it to judge a turn')
    expect(text).toContain('a chat turn ends or a check gives a verdict')
  })
})

// The card is a shrinkable column (found on the phone 2026-09-16: an
// expanded card ran past the screen with no way to scroll it): pinned
// header, a body that scrolls when the card is clamped, pinned Approve
// with the two fold toggles.
describe('AppApprovalCard — a shrinkable card', () => {
  const many = Array.from({ length: 7 }, (_, i) => ({
    id: `b${i}`, label: `Button ${i}`, type: 'send_prompt' as const, prompt: `p${i}`,
  }))

  it('pins the headline and Approve, scrolls the body, and opens both folds inside it', () => {
    renderCard(mkApp({ actions: many }))
    const card = screen.getByTestId('app-approval-card')
    expect(card.className).toContain('min-h-0')
    expect(card.className).toContain('shrink')
    expect(card.className).toContain('flex-col')
    const header = screen.getByTestId('app-card-header')
    const body = screen.getByTestId('app-card-body')
    const footer = screen.getByTestId('app-card-footer')
    expect(header.className).toContain('shrink-0')
    expect(body.className).toContain('overflow-y-auto')
    expect(body.className).toContain('min-h-0')
    expect(footer.className).toContain('shrink-0')
    expect(header.textContent).toContain('declares 7 action buttons')
    expect(body.contains(screen.getByTestId('manifest-summary'))).toBe(true)
    expect(footer.contains(screen.getByText('Approve'))).toBe(true)
    expect(footer.contains(screen.getByTestId('details-toggle'))).toBe(true)
    expect(footer.contains(screen.getByTestId('exact-manifest-toggle'))).toBe(true)
    openDetails()
    expect(body.contains(screen.getByTestId('manifest-blocks'))).toBe(true)
    expect(screen.getByTestId('manifest-blocks').className).not.toContain('overflow-hidden')
    fireEvent.click(screen.getByTestId('exact-manifest-toggle'))
    expect(body.contains(screen.getByTestId('exact-manifest'))).toBe(true)
    expect(screen.getByTestId('exact-manifest').className).not.toContain('max-h-')
    fireEvent.click(screen.getByTestId('details-toggle'))
    expect(screen.queryByTestId('manifest-blocks')).toBeNull()
  })
})

describe('meetsFloor', () => {
  it('ranks viewer < editor < manager < admin and defaults to every viewer', () => {
    expect(meetsFloor({}, 'viewer')).toBe(true)
    expect(meetsFloor({}, undefined)).toBe(true)
    expect(meetsFloor({ min_role: 'editor' }, 'viewer')).toBe(false)
    expect(meetsFloor({ min_role: 'editor' }, 'editor')).toBe(true)
    expect(meetsFloor({ min_role: 'manager' }, 'editor')).toBe(false)
    expect(meetsFloor({ min_role: 'manager' }, 'admin')).toBe(true)
  })

  // Several needed MCPs make ONE line (a template app lists every tool its
  // cards describe), the missing ones named; one MCP keeps its own line.
  it('folds the MCPs an app needs into one line and names the missing ones', () => {
    renderCard(mkApp({
      requires: { mcps: ['schedules-mcp', 'notifications-mcp', 'file-tools', 'memory-mcp', 'meetings-mcp'] },
      requires_status: {
        mcps: [
          { name: 'schedules-mcp', assigned: true }, { name: 'notifications-mcp', assigned: true },
          { name: 'file-tools', assigned: false }, { name: 'memory-mcp', assigned: true },
          { name: 'meetings-mcp', assigned: true },
        ],
        providers: [],
      },
    }))
    const line = screen.getByTestId('summary-mcps')
    expect(line.textContent).toBe('Needs the MCPs schedules-mcp, notifications-mcp, file-tools, memory-mcp and 1 more — file-tools not assigned to this agent')
    expect(line.className).toContain('text-amber-600')
    expect(screen.queryByTestId('summary-mcp-file-tools')).toBeNull()
  })

  it('says all assigned in one line, and nothing of assignment before a row exists', () => {
    renderCard(mkApp({
      requires: { mcps: ['a-mcp', 'b-mcp'] },
      requires_status: { mcps: [{ name: 'a-mcp', assigned: true }, { name: 'b-mcp', assigned: true }], providers: [] },
    }))
    expect(screen.getByTestId('summary-mcps').textContent).toBe('Needs the MCPs a-mcp and b-mcp (all assigned)')
  })
})

describe('AppApprovalCard — placed apps and the audience (SHARING.md)', () => {
  const exports = {
    methods: { status: { description: 'the status' }, 'update-project': { description: 'write one', min_role: 'editor' as const } },
    snapshots: { board: { description: 'the board' } },
  }

  it('words the audience and the per-viewer methods, and names the agents the calls answer', () => {
    renderCard(mkApp({
      manifest_empty: false, kind: 'folder',
      actions: [
        { id: 'who', label: 'Who', type: 'platform', method: 'app.audience', min_role: 'editor' },
        { id: 'r', label: 'R', type: 'platform', method: 'viewer.data.read' },
        { id: 'w', label: 'W', type: 'platform', method: 'viewer.data.write' },
      ],
      exports,
    }))
    expect(screen.getByTestId('summary-data').textContent).toBe('Uses the platform for who uses it, your saved data and saving your data')
    expect(screen.getByTestId('summary-placed-calls').textContent).toBe('Answers the agents it is placed in with the call status and the call update-project')
    openDetails()
    const body = screen.getByTestId('manifest-blocks').textContent || ''
    expect(body).toContain('asks the platform for app.audience (who uses this app: its members, the agents it is placed in')
    expect(body).toContain('asks the platform for viewer.data.write (to save your own data in this app')
    expect(body).toContain('Offers to other apps and to the agents it is placed in')
    expect(screen.getByTestId('offers-placed-note').textContent).toContain('also answer the chats and tasks of agents a share places this app in')
    expect(screen.getAllByText('editors and up').length).toBeGreaterThanOrEqual(2)
  })

  it('keeps the plain Offers words for an app that exports no call', () => {
    renderCard(mkApp({ manifest_empty: false, kind: 'folder', exports: { snapshots: exports.snapshots } }))
    expect(screen.queryByTestId('summary-placed-calls')).toBeNull()
    openDetails()
    const body = screen.getByTestId('manifest-blocks').textContent || ''
    expect(body).toContain('Offers to other apps')
    expect(body).not.toContain('placed in')
    expect(screen.queryByTestId('offers-placed-note')).toBeNull()
  })
})
