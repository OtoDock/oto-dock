import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useNavigate } from 'react-router-dom'
import { pageOfPath, tabTitle } from '../lib/ui/tabTitle'

const h = vi.hoisted(() => ({
  user: null as Record<string, unknown> | null,
}))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: h.user }) }))

import TabTitle from '../components/common/TabTitle'

const signedIn = { signedIn: true }

describe('tabTitle', () => {
  it('gives every kind of page its title', () => {
    const rows: Array<[string, string | null, string]> = [
      ['/chat/sales', 'Sales Desk', 'Sales Desk · OtoDock'],
      ['/chat/sales/0b6f6a1e-3c1f-4b8a-9a55-2b2d1c6f0e11', 'Sales Desk', 'Sales Desk · OtoDock'],
      ['/agents/sales', 'Sales Desk', 'Sales Desk · OtoDock'],
      ['/agents/sales/scheduled-tasks', 'Sales Desk', 'Sales Desk · OtoDock'],
      ['/agents/sales/mcps', 'Sales Desk', 'Sales Desk · OtoDock'],
      ['/agents', null, 'Agents · OtoDock'],
      ['/apps/app-1', 'Invoices', 'Invoices · OtoDock'],
      ['/admin', null, 'Admin · OtoDock'],
      ['/admin/users', null, 'Admin · OtoDock'],
      ['/user-settings', null, 'Settings · OtoDock'],
      ['/user-settings/ai-engines', null, 'Settings · OtoDock'],
      ['/shared/s-1', null, 'Shared chat · OtoDock'],
      ['/', null, 'OtoDock'],
      ['/runs/r-1', null, 'OtoDock'],
      ['/conversations/c-1', null, 'OtoDock'],
    ]
    for (const [path, name, want] of rows) {
      expect(tabTitle(pageOfPath(path), { ...signedIn, name }), path).toBe(want)
    }
  })

  it('says only the product before a sign-in, whatever the page', () => {
    for (const path of ['/chat/sales', '/admin/users', '/user-settings', '/apps/a', '/reset-password', '/accept-invite']) {
      expect(tabTitle(pageOfPath(path), { signedIn: false, name: 'Sales Desk' })).toBe('OtoDock')
    }
  })

  it('says only the product while a name is not loaded', () => {
    expect(tabTitle(pageOfPath('/chat/sales'), { ...signedIn, name: null })).toBe('OtoDock')
    expect(tabTitle(pageOfPath('/apps/a'), { ...signedIn, name: '   ' })).toBe('OtoDock')
  })

  it('keeps a long name short and on one line', () => {
    const t = tabTitle(pageOfPath('/chat/x'), { ...signedIn, name: `  A\nB ${'w'.repeat(200)}` })
    expect(t.startsWith('A B ')).toBe(true)
    expect(t.endsWith(' · OtoDock')).toBe(true)
    expect(t.length).toBeLessThanOrEqual(80 + ' · OtoDock'.length)
  })
})

function Nav({ to }: { to: { current: (p: string) => void } }) {
  to.current = useNavigate()
  return null
}

function mount(path: string, qc: QueryClient) {
  const nav = { current: (_p: string) => {} }
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Nav to={nav} />
        <TabTitle />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return nav
}

describe('TabTitle', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    document.title = 'OtoDock'
    h.user = { sub: 'u1', role: 'admin', agents: [] }
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('{}'))
  })
  afterEach(() => { fetchSpy.mockRestore() })

  function client() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['agents', true], [{ name: 'sales', display_name: 'Sales Desk' }, { name: 'ops', display_name: '' }])
    qc.setQueryData(['app', 'app-1'], { id: 'app-1', title: 'Invoices', slug: 'invoices' })
    return qc
  }

  it('names the agent, follows the route and fetches nothing', async () => {
    const nav = mount('/chat/sales', client())
    expect(document.title).toBe('Sales Desk · OtoDock')
    await act(async () => { nav.current('/agents/ops/config') })
    expect(document.title).toBe('ops · OtoDock')
    await act(async () => { nav.current('/apps/app-1') })
    expect(document.title).toBe('Invoices · OtoDock')
    await act(async () => { nav.current('/admin/users') })
    expect(document.title).toBe('Admin · OtoDock')
    await act(async () => { nav.current('/user-settings/general') })
    expect(document.title).toBe('Settings · OtoDock')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('follows a name the page loads later', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mount('/chat/sales', qc)
    expect(document.title).toBe('OtoDock')
    act(() => { qc.setQueryData(['agents', true], [{ name: 'sales', display_name: 'Sales Desk' }]) })
    await waitFor(() => expect(document.title).toBe('Sales Desk · OtoDock'))
  })

  it('reads the list a member sees under the member key', () => {
    h.user = { sub: 'u2', role: 'member', agents: ['sales'] }
    const qc = new QueryClient()
    qc.setQueryData(['agents', false], [{ name: 'sales', display_name: 'Sales Desk' }])
    mount('/chat/sales', qc)
    expect(document.title).toBe('Sales Desk · OtoDock')
  })

  it('says only the product signed out and during a forced step, with no fetch', () => {
    h.user = null
    mount('/chat/sales', client())
    expect(document.title).toBe('OtoDock')
    h.user = { sub: 'u1', role: 'admin', must_enroll_2fa: true }
    mount('/apps/app-1', client())
    expect(document.title).toBe('OtoDock')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('is the only writer of the title', () => {
    const root = join(__dirname, '..')
    const writers: string[] = []
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir)) {
        const p = join(dir, entry)
        if (statSync(p).isDirectory()) {
          if (entry !== 'tests') walk(p)
        } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)
            && /document\.title\s*=[^=]/.test(readFileSync(p, 'utf8'))) {
          writers.push(p.slice(root.length + 1))
        }
      }
    }
    walk(root)
    expect(writers).toEqual(['components/common/TabTitle.tsx'])
  })
})
