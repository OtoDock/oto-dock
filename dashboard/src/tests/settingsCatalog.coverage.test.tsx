// Every control a User Settings section renders has its row in the settings
// catalog (components/settings/catalog.ts), so the catalog the agent reads,
// the skill's settings map and the card in the chat know every setting a
// person can name. The rule: the six sections render with fixtures (one of
// each item), the disclosures opened by the listed clicks and never a
// dialog; a control is an element with a control role, named by its
// accessible name, else its placeholder, else the label in its row; the
// shell's and the InfoTips' buttons and the buttons that only close
// something are left out, each with its reason; a group's buttons match
// the row's options or names; a name made of fixture data maps to the
// section's item row through the fixture table; the match is against the
// section's rows alone. The reverse holds too: every row the fixtures can
// reach is found, and the rows jsdom cannot reach are listed with why.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

import { AREA, entriesOf, namesOf, type CatalogEntry } from '@/components/settings/catalog'
import * as authApi from '@/api/auth'

const { authUser } = vi.hoisted(() => ({
  authUser: { current: {} as Record<string, unknown> },
}))

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: authUser.current, setUser: vi.fn() }),
}))
vi.mock('@/hooks/useNotificationSound', () => ({
  useNotificationSound: () => ({ playPing: vi.fn(), playForSeverity: vi.fn(), stopDangerAlarm: vi.fn() }),
}))
vi.mock('@/api/webauthn', () => ({
  listPasskeys: vi.fn(async () => ({
    enabled: true, rp_host: 'localhost',
    passkeys: [{ credential_id: 'k1', name: 'Laptop', created_at: '2026-01-01', last_used: '' }],
  })),
  passkeySupported: () => true,
  registerPasskey: vi.fn(), renamePasskey: vi.fn(), deletePasskey: vi.fn(),
}))
vi.mock('@/lib/nativeBridge', () => ({ setNativeAuthInProgress: vi.fn(), isNativeApp: () => false }))
vi.mock('@/components/RemoteBadge', () => ({ default: () => null, MachineLinkNotice: () => null }))

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>
}

type Json = Record<string, unknown> | unknown[]
/** The proxy, by path prefix: the first matching prefix answers. */
function mockApi(table: Record<string, Json | (() => Json)>) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    for (const [prefix, body] of Object.entries(table)) {
      if (path.startsWith(prefix)) {
        const json = typeof body === 'function' ? body() : body
        if (init?.method === 'PUT' && init.body) return { ok: true, json: async () => JSON.parse(String(init.body)) } as Response
        return { ok: true, json: async () => json } as Response
      }
    }
    return { ok: true, json: async () => ({}) } as Response
  })
}

const MEMBER = {
  sub: 'u1', name: 'dev', display_name: 'Dev Member', email: 'member@otodock.local', role: 'member',
  auth_provider: 'local', totp_enabled: false, agent_roles: { alpha: 'viewer' }, feature_flags: {},
}

const AUDIO_PREFS = { stt_mode: 'auto', tts_mode: 'auto', tts_voice_map: {}, stt_language: null, wake_word_enabled: true }
const CAPABILITY = {
  tts: 'either', stt: 'either', tts_provider_id: 1, stt_provider_id: 1,
  reason: 'TTS: user choice. STT: user choice.', icons_enabled: true, duplex: { available: true, reason: 'ok' },
}
const WAKE_KEYWORDS = {
  enabled: true, reason: 'ok', threshold: 0.35, keywords: '▁HE Y ▁A L P HA @alpha',
  agents: [{ slug: 'alpha', display_name: 'Alpha', wakeable: true }], platform_target: 'alpha',
}

const INTEGRATION = {
  mcp_name: 'github-mcp', display_name: 'GitHub Account', description: 'Repositories and issues.',
  configured: true, required_keys: [], fields: [], oauth: true,
  oauth_services: [{ key: 'repos', label: 'Repositories', description: 'Code and pull requests.' }],
  oauth_meta: {
    provider_id: 'github', supports_multi_account: true, registered_app_required: false, bearer_required: false,
    proposed_hosts: [], flows: ['authorization_code', 'personal_access_token'], app_kind: 'github_app',
    app_install_available: true,
  },
  supports_multi_account: true, candidate_agents: ['alpha', 'beta'], reach_supported: true,
  accounts: [{
    account_label: 'work', display_email: 'dev@example.com', is_default: true, created_at: '2026-01-01',
    configured_keys: [], connected_services: ['repos'], agent_overrides: ['alpha'],
    agent_reaches: [{ agent: 'alpha', repositories: [], read_only: false }], missing_scopes: [],
    kind: 'github_app',
    installations: [{ id: 1, account: 'otodock', target_type: 'Organization', repository_selection: 'selected', repositories: ['oto-dock'], admin: true }],
  }],
}
const EVENT_CATALOG = {
  provider_id: 'github',
  event_catalog: [{ key: 'push', label: 'Push', description: 'A push to a branch.', default_selected: true }],
  vendor_target_spec: { kind: 'free_text', label: 'Repository', placeholder: 'owner/name' },
  webhook_base: '', registration: { mode: 'manual' }, per_subscription_secret: true,
}
const SUBSCRIPTION = {
  id: 'sub7', scope: 'user', owner: 'u1', agent: null, mcp_name: 'github-mcp', provider_id: 'github', account_label: 'work',
  vendor_target: 'otodock/oto-dock', vendor_subscription_id: 'v1', selected_events: ['push'], selected_subevents: {},
  status: 'active', last_error: null, last_event_at: null, event_count: 0, expires_at: null, created_by: 'u1',
  webhook_url: 'https://example.test/hook', created_at: '2026-01-01',
}
const API_KEY = { id: 'key1', name: 'GitHub webhook', prefix: 'otok_ab', permissions: ['triggers'], created_at: '2026-01-01', last_used_at: null, revoked_at: null }

const MACHINE = {
  id: 'm-0001', name: 'Work laptop', status: 'online', last_seen: null, last_heartbeat_age_s: 2, reachable: true,
  registered_by: 'u1', pairing_scope: 'user', capabilities: { os: 'linux', display: { has_display: true } },
  allow_full_fs: false, device_grants: ['computer', 'browser', 'app'], browser_mode: 'own',
  browser_extension_token_set: true, browser_unattended: false, assigned_agents: [],
}

const PERIOD = { limit: 100, used: 10, percent: 10, self_used: 2 }
const USAGE = {
  monthly: PERIOD, weekly: PERIOD, self_limits: { monthly: { ...PERIOD, limit: null }, weekly: { ...PERIOD, limit: null } },
  pool: {}, daily_chart: [{ date: '2026-01-01', cost: 1, messages: 2 }], agent_breakdown: [{ agent: 'alpha', cost: 1, messages: 2 }],
}
const POOL_CAP = {
  caps: { week_pct: null, day_pct: null, week_usd: null, day_usd: null }, on_reached: 'stop',
  engines: {
    'claude-code-cli': {
      scope: 'user', layer: 'claude-code-cli', configured: false,
      caps: { week_pct: null, day_pct: null, week_usd: null, day_usd: null },
      readings: { week_pct: 12, day_pct: 3, week_usd: 1.5, day_usd: 0.2 },
      on_reached: 'stop', accounts: 1, hits: [], warning: false, allowed: true,
    },
  },
}

/** A click that opens a disclosure, by the opener's accessible name. */
async function open(name: string | RegExp) {
  const button = await screen.findByRole('button', { name })
  await act(async () => { fireEvent.click(button) })
}

/** A click on a disclosure whose opener is found by its text (a card's
 *  title with a badge beside it). */
async function openText(text: string, selector?: string) {
  const el = await screen.findByText(text, selector ? { selector } : undefined)
  await act(async () => { fireEvent.click(el) })
}

class ObserverStub { observe() {} unobserve() {} disconnect() {} }
vi.stubGlobal('ResizeObserver', ObserverStub)

/** The roles a person acts on. */
const CONTROL_ROLES = ['button', 'checkbox', 'radio', 'switch', 'combobox', 'textbox', 'spinbutton', 'slider', 'listbox'] as const

/** What is never a setting: the InfoTip's button, a button that only
 *  closes or cancels, the shell's own rows. */
const NOT_A_SETTING: Array<[RegExp, string]> = [
  [/^About /, "an InfoTip's button, the row's explanation"],
  [/^(Cancel|Done|Back|Back to Chat|Sign out|Logout)$/, 'closes or leaves, changes nothing'],
]

const collapse = (s: string | null | undefined) => (s ?? '').replace(/\s+/g, ' ').trim()

/** The accessible name as a person's reader gives it: aria-label, the
 *  elements aria-labelledby names, the input's labels, the text. */
function accessibleName(el: HTMLElement): string {
  const label = collapse(el.getAttribute('aria-label'))
  if (label) return label
  const by = el.getAttribute('aria-labelledby')
  if (by) {
    const text = by.split(/\s+/).map((id) => collapse(document.getElementById(id)?.textContent)).filter(Boolean).join(' ')
    if (text) return text
  }
  const labels = (el as HTMLInputElement).labels
  if (labels?.length) {
    const text = [...labels].map((l) => collapse(l.textContent)).filter(Boolean).join(' ')
    if (text) return text
  }
  return collapse(el.textContent)
}

function nameOf(el: HTMLElement): string {
  const computed = accessibleName(el)
  // A glyph alone (the default star) is named by its title.
  if (computed && !/\p{L}/u.test(computed)) return el.getAttribute('title')?.trim() || computed
  if (computed) return computed
  const placeholder = el.getAttribute('placeholder')?.trim()
  if (placeholder) return placeholder
  // The label in the control's row, up to three levels above it (a field
  // whose label stands above it in its own line).
  let row: HTMLElement | null = el.closest('div')
  for (let up = 0; row && up < 3; up++, row = row.parentElement?.closest('div') ?? null) {
    const label = row.querySelector('label')?.textContent?.trim()
    if (label) return label
  }
  // Unnamed: say what it is, so the failure names it.
  return `unnamed ${el.tagName.toLowerCase()} ${el.getAttribute('type') ?? ''}`
}

function controlsOn(root: HTMLElement): string[] {
  const names: string[] = []
  for (const role of CONTROL_ROLES) {
    for (const el of screen.queryAllByRole(role, { hidden: false })) {
      if (!root.contains(el)) continue
      if (el.closest('[aria-hidden="true"]')) continue
      names.push(nameOf(el))
    }
  }
  // A password field has no role of its own.
  for (const el of root.querySelectorAll<HTMLElement>('input[type="password"]')) {
    if (!el.closest('[aria-hidden="true"]')) names.push(nameOf(el))
  }
  return names
}

/** A control name made of fixture data, mapped to its catalog row. */
type Fixtures = Record<string, string>

function unmatched(section: string, names: string[], fixtures: Fixtures): string[] {
  const rows = entriesOf(AREA.USER, section)
  const known = new Set(rows.flatMap((row) => [...namesOf(row), ...(row.options ?? []).map((o) => o.value)]))
  const out: string[] = []
  for (const name of names) {
    if (NOT_A_SETTING.some(([re]) => re.test(name))) continue
    if (known.has(name)) continue
    // A fixture name opens a control's text (a card's title runs into its badge).
    if (Object.keys(fixtures).some((f) => name.startsWith(f))) continue
    out.push(name)
  }
  return out
}

/** The rows the fixtures do not reach, each with why. A fixture name
 *  counts for its row only when a seen control's name opens with it. */
function unreached(section: string, names: string[], fixtures: Fixtures, beyond: Record<string, string>): string[] {
  const seen = new Set(names)
  const byFixture = new Set(
    Object.entries(fixtures).filter(([f]) => names.some((n) => n.startsWith(f))).map(([, id]) => id),
  )
  return entriesOf(AREA.USER, section)
    .filter((row: CatalogEntry) => !(row.id in beyond) && !byFixture.has(row.id)
      && ![...namesOf(row), ...(row.options ?? []).map((o) => o.value)].some((n) => seen.has(n)))
    .map((row) => row.id)
}

/** The controls seen on a section across its clicks: a disclosure that
 *  opens may hide the button that opened it. */
function seen(root: HTMLElement) {
  const names = new Set<string>()
  return {
    snap: () => { for (const n of controlsOn(root)) names.add(n) },
    names: () => [...names],
  }
}

/** What the fixtures never render, so the test cannot see its controls: a
 *  guard for the controls a section shows at rest, not for every state. A
 *  state driven here moves off this list. */
const UNDRIVEN: Record<string, string> = {
  'the TOTP set-up and its removal': 'the set-up calls the proxy before its box shows (user.general.authenticator-app)',
  'the add-account flow and the plain-credentials form': 'a dialog over the Integrations page (user.integrations.add-account)',
  'the pair dialog and an admin-paired machine': 'a dialog, and a row the fixture does not hold (user.remote-machines.pair-machine)',
  'every engine sign-in step and every engine but Claude': 'the fixture holds one engine at rest (user.ai-engines.connect)',
  'the device voice and language': 'jsdom has no speech voices (user.audio.device-voice)',
  'the OIDC and Android variants of General': 'the fixture signs a local person in on a browser',
}

function check(section: string, names: string[], fixtures: Fixtures, beyond: Record<string, string>) {
  expect(Object.keys(UNDRIVEN).length).toBeGreaterThan(0)
  expect(names.length).toBeGreaterThan(3)
  expect(unmatched(section, names, fixtures), `controls on ${section} with no catalog row; add the row to catalog.ts`).toEqual([])
  expect(unreached(section, names, fixtures, beyond), `catalog rows of ${section} the fixtures did not reach; fix the fixture or list the row with why`).toEqual([])
}

afterEach(() => { fetchSpy.mockReset() })

describe('the settings catalog covers the User Settings sections', () => {
  it('General', async () => {
    authUser.current = MEMBER
    ;(window as unknown as { PublicKeyCredential: unknown }).PublicKeyCredential = class {}
    mockApi({
      '/v1/users/me/audio-prefs': AUDIO_PREFS, '/v1/audio/capability': CAPABILITY,
      '/v1/users/me/wake-keywords': WAKE_KEYWORDS, '/v1/users/me/ui-prefs': {},
    })
    const { GeneralSection } = await import('@/pages/UserSettings')
    const { container } = render(<GeneralSection />, { wrapper })
    const view = seen(container)
    await screen.findByText('Laptop')
    view.snap()
    await open('Troubleshooting')
    await open(/Add another passkey/)
    view.snap()
    await open('Rename')
    view.snap()
    check('general', view.names(), {}, {
      'user.general.role': 'a reading, no control',
    })
  })

  it('Integrations', async () => {
    authUser.current = MEMBER
    mockApi({
      '/v1/users/me/integrations': [INTEGRATION], '/v1/user-api-keys': { keys: [API_KEY] },
      '/v1/subscriptions': { subscriptions: [SUBSCRIPTION] }, '/v1/mcps/github-mcp/webhook-event-catalog': EVENT_CATALOG,
      '/v1/oauth/github/accounts': { accounts: [], has_service_credentials_only: false },
    })
    const { IntegrationsSection } = await import('@/pages/UserSettings')
    const { container } = render(<IntegrationsSection />, { wrapper })
    const view = seen(container)
    await openText('GitHub Account')
    await screen.findByText('dev@example.com')
    await screen.findByText('otodock/oto-dock')
    view.snap()
    await open('Edit')
    view.snap()
    check('integrations', view.names(), {
      'GitHub Account': 'user.integrations.account',
      'alpha': 'user.integrations.agent-binding', 'beta': 'user.integrations.agent-binding',
      'Change repositories for': 'user.integrations.agent-binding', 'Read only for': 'user.integrations.agent-binding',
      'Repositories': 'user.integrations.account',
    }, {})
  })

  it('Remote Machines', async () => {
    authUser.current = MEMBER
    mockApi({
      '/v1/users/me/remote-machines': { machines: [MACHINE], targets: [] },
    })
    const { MyMachinesSection } = await import('@/pages/UserSettings.machines')
    const { container } = render(<MyMachinesSection />, { wrapper })
    const view = seen(container)
    await openText('Work laptop')
    view.snap()
    await open('Remove machine')
    view.snap()
    check('remote-machines', view.names(), {
      'Work laptop': 'user.remote-machines.remove-machine',
      'alphaviewer': 'user.remote-machines.run-agents',
    }, {})
  })

  it('AI Engines', async () => {
    authUser.current = { ...MEMBER, role: 'admin' }
    const { claudeLike } = await import('./fixtures/engines')
    mockApi({
      '/v1/users/me/execution-layers': { layers: [{
        name: 'claude-code-cli', display_name: 'Anthropic Claude Code',
        capabilities: claudeLike({ display_name: 'Anthropic Claude Code' }),
        user_subscriptions: [{ id: 's1', label: 'Claude Max', oauth_email: 'a@example.com', auth_type: 'oauth', status: 'active', use_personal: true, contribute_platform: false }],
        platform_available: true, allow_platform_auth: true,
      }] },
    })
    const { ExecutionLayersSection } = await import('@/pages/UserSettings.aiEngines')
    const { container } = render(<ExecutionLayersSection />, { wrapper })
    const view = seen(container)
    await openText('Claude Code', 'span')
    view.snap()
    await open('Use an API key instead')
    view.snap()
    check('ai-engines', view.names(), {
      'AnthropicClaude Code': 'user.ai-engines.subscription',
    }, {})
  })

  it('Audio', async () => {
    authUser.current = MEMBER
    mockApi({ '/v1/users/me/audio-prefs': AUDIO_PREFS, '/v1/audio/capability': CAPABILITY })
    const { AudioPrefsSection } = await import('@/components/audio/AudioPrefsSection')
    const { container } = render(<AudioPrefsSection />, { wrapper })
    await screen.findByText('Voice playback')
    check('audio', controlsOn(container), {}, {
      'user.audio.device-voice': 'jsdom has no speech voices',
    })
  })

  it('Usage', async () => {
    authUser.current = MEMBER
    const { claudeLike } = await import('./fixtures/engines')
    mockApi({
      '/v1/usage/me/limits': { limits: [] }, '/v1/usage/me/pool-cap': POOL_CAP, '/v1/usage/me': USAGE,
      '/v1/execution-layers': { 'claude-code-cli': claudeLike({ display_name: 'Claude Code' }) },
    })
    const { UsageSection } = await import('@/pages/UserSettings.usage')
    const { container } = render(<UsageSection />, { wrapper })
    const view = seen(container)
    await screen.findByText('My API keys')
    await waitFor(() => expect(screen.queryByText('Loading...')).toBeNull())
    view.snap()
    await open('Set cap')
    view.snap()
    check('usage', view.names(), {}, {
      'user.usage.summary': 'a reading, no control',
    })
  })
})
