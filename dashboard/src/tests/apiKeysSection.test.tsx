// The API keys in User Settings: what the mint sends, the confirmation it may
// ask for, the key shown once, and a revocation's list of what the key made.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/http/apiJson'

const apiJson = vi.fn()
vi.mock('@/lib/http/apiJson', async (original) => ({
  ...(await original<typeof import('@/lib/http/apiJson')>()),
  apiJson: (...args: unknown[]) => apiJson(...args),
}))
vi.mock('@/api/agents', () => ({
  useAgents: () => ({ data: [{ name: 'pa', display_name: 'Assistant' }] }),
}))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: { sub: 'u1' } }) }))
vi.mock('@/components/ui/dialogs', () => ({ useDialogs: () => ({ confirm: async () => true }) }))
const copyOrSay = vi.fn(async (_text: string) => true)
vi.mock('@/lib/copyText', () => ({ copyOrSay: (t: string) => copyOrSay(t), copyText: copyOrSay }))

const KEY = {
  id: 'k1', name: 'Report', prefix: 'abc', permissions: ['chat'], agents: ['pa'],
  expires_at: '2036-01-01T00:00:00+00:00', run_without_asking: false, created_at: '2026-01-01',
  last_used_at: null, revoked_at: null,
}

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>
}

async function section() {
  const { ApiKeysSection } = await import('@/pages/UserSettings.apiKeys')
  return render(<ApiKeysSection />, { wrapper })
}

async function adminCard() {
  const { default: ApiKeysCard } = await import('@/pages/admin/ApiKeysCard')
  return render(<ApiKeysCard />, { wrapper })
}

describe('the admin\'s API keys card', () => {
  beforeEach(() => { apiJson.mockReset() })

  it('lists a person\'s key and an agent\'s key, and revokes by kind', async () => {
    const calls: string[] = []
    apiJson.mockImplementation(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? 'GET'} ${url}`)
      if (init?.method === 'DELETE') return { status: 'revoked', id: 'a1', created_total: 0, created: [] }
      return {
        enabled: false,
        keys: [
          { ...KEY, kind: 'user', owner: 'Ada', owner_email: 'ada@x' },
          { id: 'a1', kind: 'agent', name: 'Hook', prefix: 'p', permissions: ['triggers'], agent: 'pa', created_at: '', last_used_at: null, revoked_at: null },
        ],
      }
    })
    await adminCard()
    expect(await screen.findByText('Ada')).toBeTruthy()
    expect(screen.getByText('Agent pa')).toBeTruthy()
    // The switch shows what the server said, never a guess.
    expect((screen.getByRole('switch', { name: 'Allow API keys' }) as HTMLInputElement).getAttribute('aria-checked')).toBe('false')
    fireEvent.click(screen.getAllByText('Revoke')[1])
    await waitFor(() => expect(calls).toContain('DELETE /v1/admin/api-keys/agent/a1'))
  })

  it('shows no switch while it does not know the setting', async () => {
    apiJson.mockRejectedValue(new Error('Could not load the API keys'))
    await adminCard()
    expect(await screen.findByText('Could not load the API keys')).toBeTruthy()
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('says whose keys these are: people\'s and agents\'', async () => {
    apiJson.mockResolvedValue({ enabled: true, keys: [] })
    await adminCard()
    fireEvent.click(await screen.findByRole('button', { name: 'About API Keys' }))
    const tip = await screen.findByRole('tooltip')
    expect(tip.textContent).toContain('An agent\'s managers make its webhook keys')
    expect(tip.textContent).not.toContain('A key acts as its person')
  })
})

describe('API keys in User Settings', () => {
  beforeEach(() => {
    apiJson.mockReset()
    copyOrSay.mockClear()
  })

  it('lists what a key reaches and when it ends', async () => {
    apiJson.mockResolvedValue({ keys: [KEY, { ...KEY, id: 'k2', name: 'Old', agents: [], expires_at: null, permissions: ['triggers'] }] })
    await section()
    expect(await screen.findByText('Report')).toBeTruthy()
    expect(screen.getByText(/pa · ends/)).toBeTruthy()
    expect(screen.getByText(/All your agents · made before keys had an end date/)).toBeTruthy()
  })

  it('asks for the password when the server does, and shows the key once with its Copy', async () => {
    apiJson.mockImplementation(async (url: string, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'GET') return { keys: [] }
      const body = JSON.parse(String(init?.body))
      if (!body.password) {
        throw new ApiError(428, 'Enter your password to confirm', { code: 'confirm_required', method: 'password', message: 'Enter your password to confirm' }, {})
      }
      return { ...KEY, key: 'otok_shown_once' }
    })
    await section()
    fireEvent.click(await screen.findByText('+ Create key'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Report' } })
    fireEvent.click(screen.getByLabelText('Only these'))
    fireEvent.click(screen.getByLabelText('Assistant'))
    fireEvent.click(screen.getByText('Create'))
    const password = await screen.findByLabelText('Confirm it is you')
    fireEvent.change(password, { target: { value: 'secret' } })
    fireEvent.click(screen.getByText('Create'))
    expect((await screen.findByTestId('created-api-key')).textContent).toBe('otok_shown_once')
    const posts = apiJson.mock.calls.filter((c) => c[1]?.method === 'POST')
    const body = JSON.parse(String((posts[posts.length - 1][1] as RequestInit).body))
    expect(body).toMatchObject({ name: 'Report', permissions: ['chat'], agents: ['pa'], expires_in_days: 30, run_without_asking: false, password: 'secret' })
    fireEvent.click(screen.getByText('Copy to clipboard'))
    expect(copyOrSay).toHaveBeenCalledWith('otok_shown_once')
  })

  it('says what a key that asks still runs without asking', async () => {
    apiJson.mockResolvedValue({ keys: [] })
    await section()
    fireEvent.click(await screen.findByText('+ Create key'))
    const said = screen.getByTestId('ask-still-runs').textContent ?? ''
    // Ask is where the key sends, and what a person approves there.
    expect(said).toContain('Ask or Plan')
    expect(said).toContain('you approve each step that changes something')
    // The four things that run there with nobody asked.
    for (const thing of [
      'The agent reads.',
      "a paired computer's browser or screen",
      'the tools that never ask',
      'a tool you allowed earlier in the same turn',
    ]) expect(said, thing).toContain(thing)
    // Short sentences, and none of the platform's inner words.
    for (const sentence of said.split('. ')) expect(sentence.split(' ').length, sentence).toBeLessThanOrEqual(16)
    expect(said).not.toMatch(/card|tier|session|hook|token|grant/i)
  })

  it('offers Tasks only on a key that may run without asking, and says why', async () => {
    apiJson.mockImplementation(async (_url: string, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'GET') return { keys: [] }
      return { ...KEY, key: 'otok_shown_once' }
    })
    await section()
    fireEvent.click(await screen.findByText('+ Create key'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Nightly' } })
    const tasks = screen.getByRole('checkbox', { name: /Run your tasks/ }) as HTMLInputElement
    const unasked = screen.getByRole('checkbox', { name: /May run without asking/ }) as HTMLInputElement
    expect(tasks.disabled).toBe(true)
    expect(screen.getByTestId('tasks-needs-unasked').textContent).toContain('nobody to ask')
    fireEvent.click(unasked)
    expect(tasks.disabled).toBe(false)
    expect(screen.queryByTestId('tasks-needs-unasked')).toBeNull()
    fireEvent.click(tasks)
    expect(tasks.checked).toBe(true)
    // Taking the flag back takes Tasks with it.
    fireEvent.click(unasked)
    expect(tasks.checked).toBe(false)
    expect(tasks.disabled).toBe(true)
    fireEvent.click(screen.getByText('Create'))
    await screen.findByTestId('created-api-key')
    const posts = apiJson.mock.calls.filter((c) => c[1]?.method === 'POST')
    const body = JSON.parse(String((posts[posts.length - 1][1] as RequestInit).body))
    expect(body).toMatchObject({ permissions: ['chat'], run_without_asking: false })
  })

  it('says what a revoked key made, one thing or several', async () => {
    const { RevokedKeyDialog } = await import('@/pages/UserSettings.apiKeys')
    const made = [{ kind: 'chat', ref: 'c1', label: 'A new chat', agent: 'pa', created_at: '' }]
    const one = render(<RevokedKeyDialog result={{ status: 'revoked', id: 'k', created_total: 1, created: made }} onClose={() => {}} />)
    expect(screen.getByText(/It made one thing, which stays\./)).toBeTruthy()
    one.unmount()
    render(<RevokedKeyDialog result={{ status: 'revoked', id: 'k', created_total: 3, created: made }} onClose={() => {}} />)
    expect(screen.getByText(/It made 3 things, and they stay\./)).toBeTruthy()
  })

  it('lists what a revoked key made', async () => {
    apiJson.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') {
        return { status: 'revoked', id: 'k1', created_total: 1, created: [{ kind: 'task', ref: 't1', label: 'Nightly', agent: 'pa', created_at: '' }] }
      }
      return { keys: [KEY] }
    })
    await section()
    fireEvent.click(await screen.findByText('Revoke'))
    await waitFor(() => expect(screen.getByTestId('key-created-list').textContent).toContain('Nightly'))
  })
})
