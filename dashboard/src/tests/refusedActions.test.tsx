// An action a person starts that the server refuses shows a toast with the
// server's sentence: a memory switch, a memory clear, a trigger's pause and
// resume (the brief's named sites; the structural guard in
// refusalSentences.test.tsx holds every other one).
import type { ReactElement } from 'react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { personClick, renderWithDialogs } from './dialogsHarness'
import { onAppToast, type AppToast } from '@/lib/appToasts'

const h = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/auth')>()),
  apiFetch: (...args: unknown[]) => h.apiFetch(...args),
}))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { sub: 'u1', role: 'admin', agents: ['ag'], agent_roles: { ag: 'manager' }, feature_flags: {} },
    refreshUser: vi.fn(async () => {}),
  }),
}))

import { MemorySection } from '@/pages/agent/AgentConfig.memory'
import AgentTriggers from '@/pages/agent/AgentTriggers'

const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

/** Answers GETs from `reads` and refuses every write with `detail`. */
function server(reads: Record<string, unknown>, status: number, detail: string) {
  h.apiFetch.mockImplementation((url?: string, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method === 'GET') {
      const key = Object.keys(reads).find((k) => String(url ?? '').startsWith(k))
      return key ? json(reads[key]) : json({ detail: 'Not found' }, 404)
    }
    return json({ detail }, status)
  })
}

async function toastsOf(run: () => Promise<void>): Promise<AppToast[]> {
  const seen: AppToast[] = []
  const stop = onAppToast((t) => seen.push(t))
  try { await run() } finally { stop() }
  return seen
}

function renderIn(ui: ReactElement, path = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  renderWithDialogs(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes><Route path="*" element={ui} /><Route path="/agents/:name/triggers" element={ui} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => h.apiFetch.mockReset())

describe('the memory settings', () => {
  const settings = { agent: 'ag', user_memory_enabled: true, agent_memory_enabled: true }

  it('a refused switch says why', async () => {
    server({ '/v1/internal/memory/agent-settings/ag': settings }, 403,
      'Only a manager of this agent can change its memory settings.')
    renderIn(<MemorySection name="ag" mode="personal_shared" />)
    const toasts = await toastsOf(async () => {
      personClick(await screen.findByRole('switch', { name: 'User memory' }))
      await waitFor(() => expect(h.apiFetch).toHaveBeenCalledWith(
        '/v1/internal/memory/agent-settings/ag', expect.objectContaining({ method: 'PATCH' })))
      await new Promise((r) => setTimeout(r, 20))
    })
    expect(toasts.map((t) => [t.title, t.body])).toEqual([
      ['The memory setting was not saved', 'Only a manager of this agent can change its memory settings.']])
  })

  it('a refused clear says why and keeps the confirm open', async () => {
    server({ '/v1/internal/memory/agent-settings/ag': settings }, 404, 'This agent no longer exists.')
    renderIn(<MemorySection name="ag" mode="personal_shared" />)
    personClick(await screen.findByRole('button', { name: 'Clear shared agent memory' }))
    fireEvent.change(await screen.findByLabelText('Type CLEAR-AGENT-MEMORY to confirm'),
      { target: { value: 'CLEAR-AGENT-MEMORY' } })
    const toasts = await toastsOf(async () => {
      personClick(screen.getByRole('button', { name: 'Clear memory' }))
      await waitFor(() => expect(h.apiFetch).toHaveBeenCalledWith(
        '/v1/internal/memory/clear-agent-memory/ag', expect.objectContaining({ method: 'POST' })))
      await new Promise((r) => setTimeout(r, 20))
    })
    expect(toasts.map((t) => [t.title, t.body])).toEqual([
      ['The memory was not cleared', 'This agent no longer exists.']])
    expect(screen.getByLabelText('Type CLEAR-AGENT-MEMORY to confirm')).toBeInTheDocument()
  })
})

describe('a trigger card', () => {
  const trigger = (enabled: boolean) => ({
    id: 't1', slug: 'deploy', name: 'Deploy hook', scope: 'agent', agent: 'ag', created_by: 'u1',
    task_id: null, notify_enabled: true, notify_severity: 'info', notify_title: 'Deployed', notify_body: null,
    notify_target_scope: 'agent', notify_target: null, debounce_seconds: 0, enabled, fired_count: 0,
    last_fired_at: null, last_error: null, subscription_id: null, event_filter: null,
    created_at: '2026-10-18T00:00:00Z', updated_at: '2026-10-18T00:00:00Z',
    can_edit: false, can_delete: false, can_pause: enabled, can_resume: !enabled, can_fire: false,
  })

  it.each([
    ['Pause', true, 'The trigger was not paused'],
    ['Resume', false, 'The trigger was not resumed'],
  ])('a refused %s says why', async (button, enabled, title) => {
    server({ '/v1/triggers': { triggers: [trigger(enabled)] }, '/v1/agents/ag/api-keys': { keys: [] } },
      404, 'This trigger no longer exists.')
    renderIn(<AgentTriggers />, '/agents/ag/triggers')
    const toasts = await toastsOf(async () => {
      personClick(await screen.findByRole('button', { name: button }))
      await waitFor(() => expect(h.apiFetch).toHaveBeenCalledWith(
        `/v1/triggers/t1/${button.toLowerCase()}`, expect.objectContaining({ method: 'POST' })))
      await new Promise((r) => setTimeout(r, 20))
    })
    expect(toasts.map((t) => [t.title, t.body])).toEqual([[title, 'This trigger no longer exists.']])
  })
})
