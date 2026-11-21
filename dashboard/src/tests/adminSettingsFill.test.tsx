// Admin → Setup: a tab opened when the settings are already loaded (through
// the tab bar after another tab, or a quick direct load) shows the saved
// values, never the form's own defaults, and leaving a field untouched saves
// nothing. A form that showed its defaults saved them over the admin's values
// when a field was left (a session timeout of 3600 over 7200, a password rule
// of 8 characters over 12, an empty company name or Turnstile key).
// The settings below differ from every default and are there from the first
// render, the same object on every render, as the query's cached data is.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderWithDialogs } from './dialogsHarness'

const h = vi.hoisted(() => ({
  save: vi.fn(async (_data: unknown): Promise<unknown> => ({})),
  settings: {
    cloud: false, forced_keys: [] as string[],
    company_name: 'Acme', platform_instructions: 'Answer in Greek.',
    platform_timezone: 'Europe/Athens', session_timeout: '7200', session_idle_timeout: '1800',
    jwt_expiry_hours: '24', allow_user_paired_machines: true, remote_fallback_user_override: true,
    remote_fallback_agent_default: false, interactive_cli_enabled: true,
    session_retention_enabled: false, session_retention_days: '30',
    working_files_retention_enabled: true, working_files_retention_days: '90',
    offboarded_retention_enabled: true, offboarded_retention_days: '60',
    mcp_auto_update_enabled: true, subscription_windows_enabled: true,
    quota_shared_folder_mb: '15360', quota_user_folder_mb: '2048',
    quota_shared_folder_inodes: '0', quota_user_folder_inodes: '0',
    smtp_host: 'mail.example.com', smtp_port: '2525', smtp_user: 'robot', smtp_from: 'robot@example.com',
    smtp_tls: 'true', smtp_password_set: true,
    turnstile_site_key: '0x4AAA-acme', turnstile_secret_key_set: true, turnstile_managed: false,
    password_min_score: '4', password_min_length: '12',
    require_2fa: false, passkey_login_mode: 'passwordless',
    sharing_external_enabled: true, sharing_public_links_enabled: true, sharing_max_expiry_days: '',
    user_directory_visible_to_members: true, sharing_to_agents_enabled: true,
    sharing_to_departments_enabled: true, forwarding_warnings: [],
  },
}))

vi.mock('@/api/auth', () => ({ apiFetch: vi.fn(async () => new Response('{}')) }))
vi.mock('@/pages/admin/ApiKeysCard', () => ({ default: () => null }))
vi.mock('@/api/oauth', () => ({
  useBearerAllowlist: () => ({ data: [], isLoading: false }),
  useBearerPending: () => ({ data: [] }),
  useAllowMcpHosts: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useAddBearerAllowlist: () => ({ mutateAsync: vi.fn() }),
  useRemoveBearerAllowlist: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve()) }),
  useRemoveAdminBearerRows: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve()), isPending: false }),
}))
vi.mock('@/pages/admin/PlatformPage.hooks', () => ({
  usePlatformSettings: () => ({ data: h.settings, isLoading: false }),
  useSavePlatformSettings: () => ({ mutate: vi.fn(), mutateAsync: h.save, isPending: false }),
  useConcurrencyStats: () => ({ data: undefined }),
  useStorageUsage: () => ({ data: undefined }),
}))
vi.mock('@/api/memory', () => ({
  useMemorySettings: () => ({ data: undefined }),
  useUpdateMemorySettings: () => ({ mutateAsync: vi.fn() }),
}))
vi.mock('@/api/titleGeneration', () => ({
  useTitleGeneration: () => ({ data: undefined, isLoading: true }),
  useSaveTitleGeneration: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useCheckTitleGeneration: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}))
vi.mock('@/api/mcps', () => ({ useMcpAutoUpdateLog: () => ({ data: { runs: [], last_run_at: '' } }) }))
vi.mock('@/lib/appToasts', () => ({ emitAppToast: vi.fn(), onAppToast: () => () => {} }))

import GeneralTab from '@/pages/admin/GeneralTab'
import SecurityTab from '@/pages/admin/SecurityTab'
import SystemSettingsTab from '@/pages/admin/SystemSettingsTab'

function mountSystem() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(<QueryClientProvider client={qc}><SystemSettingsTab /></QueryClientProvider>)
}

/** Each field shows its saved value, and leaving it untouched saves nothing. */
function expectSavedAndQuiet(values: string[]) {
  for (const value of values) {
    const field = screen.getByDisplayValue(value)
    fireEvent.focus(field)
    fireEvent.blur(field)
  }
  expect(h.save).not.toHaveBeenCalled()
}

beforeEach(() => {
  h.save.mockClear()
  h.settings.forced_keys = []
})

describe('an admin tab opened with the settings already loaded', () => {
  it('General shows the saved company name and instructions', () => {
    render(<GeneralTab />)
    expectSavedAndQuiet(['Acme', 'Answer in Greek.'])
  })

  it('Security shows the saved email server, Turnstile key and password rule', () => {
    render(<SecurityTab />)
    expect((screen.getByRole('option', { name: /Very strong \(4\)/ }) as HTMLOptionElement).selected).toBe(true)
    expectSavedAndQuiet(['mail.example.com', '2525', 'robot', 'robot@example.com', '0x4AAA-acme', '12'])
    const score = screen.getByRole('option', { name: /Very strong \(4\)/ }).closest('select')!
    fireEvent.blur(score)
    expect(h.save).not.toHaveBeenCalled()
  })

  it('System shows the saved timeouts and retention', () => {
    mountSystem()
    expectSavedAndQuiet(['7200', '24', '1800', '30', '90', '60'])
    expect(screen.getByRole('checkbox', { name: 'Clean up old chat session files' })).not.toBeChecked()
  })
})

describe('a forced session-files cleanup', () => {
  it('locks the switch and its days', () => {
    h.settings.forced_keys = ['session_retention_enabled', 'session_retention_days']
    mountSystem()
    expect(screen.getByRole('checkbox', { name: 'Clean up old chat session files' })).toBeDisabled()
    expect(screen.getByLabelText('Days to keep session files')).toBeDisabled()
  })

  it('locks the days alone when only they are forced', () => {
    h.settings.forced_keys = ['session_retention_days']
    h.settings.session_retention_enabled = true
    try {
      mountSystem()
      expect(screen.getByRole('checkbox', { name: 'Clean up old chat session files' })).toBeEnabled()
      expect(screen.getByLabelText('Days to keep session files')).toBeDisabled()
    } finally {
      h.settings.session_retention_enabled = false
    }
  })
})
