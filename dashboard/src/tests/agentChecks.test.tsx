import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// ─── The Checks page (CHECKS.md "Governance"): the agent's checks with their
//     mandatory/offered badge, the caller's own checks, the manager-only
//     "New check" and spend cap, the verdict table; the editor's shape ───

let role = 'admin'
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role, agent_roles: {} } }),
}))

import * as authApi from '@/api/auth'
import AgentChecks from '@/pages/agent/AgentChecks'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const CHECKS = {
  can_manage: true, username: 'dev-admin',
  checks: [
    {
      ref: 'agent:coding', name: 'coding', owner: '', description: 'Lint, then a judge on the diff.',
      mandatory: true, applies: ['chats', 'tasks', 'delegations'], condition: { kinds: ['code'], events: ['commit'] },
      rounds: 3, inputs: [], sections: ['script', 'judge'], doc: { name: 'coding' }, updated_by: 'file',
      updated_at: '2026-09-17T09:00:00Z',
    },
    {
      ref: 'agent:style', name: 'style', owner: '', description: 'The house style.', mandatory: false,
      applies: ['chats'], condition: {}, rounds: 0, inputs: ['knowledge/style.md'], sections: ['judge'],
      problems: ['the rubric is empty'],
    },
    {
      ref: 'user:review', name: 'review', owner: 'dev-admin', description: 'My own review.', mandatory: false,
      applies: ['chats'], condition: { always: true }, rounds: 1, inputs: [], sections: ['judge'],
    },
  ],
}

const VERDICTS = {
  verdicts: [
    {
      id: 'v1', agent: 'dev', owner: '', check_name: 'coding', section: 'judge', status: 'fail', pass: false,
      score: 0.3, findings: [{ location: 'a.py', severity: 'error', text: 'missing test' }],
      summary: 'One file has no test.', reason: '', session_id: 's', chat_id: 'c-one', run_id: '', judge_run_id: 'j',
      user_sub: 'u1', round: 1, ran_on: 'local', engine: 'claude-code-cli', model: 'sonnet', cost_usd: 0.5,
      duration_ms: 4000, created_at: '2026-09-17T09:30:00Z',
    },
  ],
}

// The pages: the first answer carries one row past the page (an older page
// exists), the answer to a `before` cursor carries the rest.
const verdictPaths: string[] = []
const OLDER = { verdicts: [{ ...VERDICTS.verdicts[0], id: 'v-old', summary: 'An older verdict.', created_at: '2026-09-10T09:30:00Z' }] }

function stub(data: typeof CHECKS = CHECKS) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.includes('/check-verdicts?')) {
      verdictPaths.push(path)
      return { ok: true, json: async () => (path.includes('before=') ? OLDER : VERDICTS) } as Response
    }
    if (path.endsWith('/check-settings')) return { ok: true, json: async () => ({ agent: 'dev', daily_cap_usd: null, platform_default_usd: null, can_manage: true }) } as Response
    if (path.includes('/checks/') || path.includes('/user-checks/')) {
      return { ok: true, json: async () => ({ ok: true, method: init?.method }) } as Response
    }
    if (path.endsWith('/checks')) return { ok: true, json: async () => data } as Response
    return { ok: true, json: async () => ({}) } as Response
  })
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/agents/dev/checks']}>
        <Routes>
          <Route path="/agents/:name/checks" element={<AgentChecks />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AgentChecks', () => {
  afterEach(() => { fetchSpy.mockReset(); role = 'admin' })

  it('lists the agent checks with their badge, the private ones apart, and the problems', async () => {
    stub()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('check-agent:coding')).toBeTruthy())
    expect(screen.getByTestId('check-agent:coding').textContent).toContain('mandatory')
    expect(screen.getByTestId('check-agent:coding').textContent).toContain('writes code; on a commit')
    expect(screen.getByTestId('check-agent:style').textContent).toContain('offered')
    expect(screen.getByTestId('check-agent:style').textContent).toContain('needs a fix')
    expect(screen.getByTestId('check-agent:style').textContent).toContain('reports only')
    expect(screen.getByTestId('check-user:review').textContent).toContain('mine')
    expect(screen.getByTestId('check-user:review').textContent).toContain('every turn')
    // Managers get the New check button and the spend cap.
    expect(screen.getByTestId('new-check')).toBeTruthy()
    expect(screen.getByTestId('check-spend')).toBeTruthy()
    // The verdict table shows the check, the verdict and the first finding.
    await waitFor(() => expect(screen.getByTestId('check-verdicts').textContent).toContain('One file has no test.'))
    expect(screen.getByTestId('check-verdicts').textContent).toContain('missing test')
    expect(screen.getByTestId('check-verdicts').textContent).toContain('$0.50')
  })

  // The list is paged (2026-09-18: an agent judged every turn fills a page
  // a day): twenty rows a page, "Older" asks for the rows before the last
  // one shown, "Newer" walks back without a fetch of a new shape.
  it('pages the verdicts twenty at a time with Older and Newer', async () => {
    const many = Array.from({ length: 21 }, (_, i) => ({
      ...VERDICTS.verdicts[0], id: `v${i}`, summary: `Verdict ${i}.`,
      created_at: `2026-09-17T${String(23 - i).padStart(2, '0')}:00:00Z`,
    }))
    verdictPaths.length = 0
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.includes('/check-verdicts?')) {
        verdictPaths.push(path)
        return { ok: true, json: async () => (path.includes('before=') ? OLDER : { verdicts: many }) } as Response
      }
      if (path.endsWith('/check-settings')) return { ok: true, json: async () => ({ agent: 'dev', daily_cap_usd: null, platform_default_usd: null, can_manage: true }) } as Response
      if (path.endsWith('/checks')) return { ok: true, json: async () => CHECKS } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('check-verdicts').textContent).toContain('Verdict 0.'))
    const list = screen.getByTestId('check-verdicts')
    expect(list.querySelectorAll('tbody tr')).toHaveLength(20)
    expect(list.textContent).not.toContain('Verdict 20.')
    expect(verdictPaths[0]).toContain('limit=21')
    expect(verdictPaths[0]).not.toContain('before=')
    const older = screen.getByTestId('verdicts-older') as HTMLButtonElement
    expect(older.disabled).toBe(false)
    expect((screen.getByTestId('verdicts-newer') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(older)
    await waitFor(() => expect(screen.getByTestId('check-verdicts').textContent).toContain('An older verdict.'))
    expect(verdictPaths[verdictPaths.length - 1]).toContain(`before=${encodeURIComponent('2026-09-17T04:00:00Z')}`)
    expect(screen.getByTestId('verdict-pages').textContent).toContain('page 2')
    expect((screen.getByTestId('verdicts-older') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByTestId('verdicts-newer'))
    await waitFor(() => expect(screen.getByTestId('check-verdicts').textContent).toContain('Verdict 0.'))
  })

  it('says when the checks tool is off for the agent, with the MCPs link for a manager', async () => {
    stub({ ...CHECKS, tool_enabled: false } as typeof CHECKS)
    renderPage()
    await waitFor(() => expect(screen.getByTestId('checks-tool-off')).toBeTruthy())
    const hint = screen.getByTestId('checks-tool-off')
    expect(hint.textContent).toContain('The checks tool is off for this agent')
    expect(hint.querySelector('a')?.getAttribute('href')).toBe('/agents/dev/mcps')
  })

  it('hides the manager controls from a member but keeps the private check button', async () => {
    role = 'member'
    stub({ ...CHECKS, can_manage: false, checks: CHECKS.checks.filter((c) => !c.mandatory || true) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('check-agent:coding')).toBeTruthy())
    expect(screen.queryByTestId('new-check')).toBeNull()
    expect(screen.queryByTestId('check-spend')).toBeNull()
    expect(screen.getByTestId('new-private-check')).toBeTruthy()
    // No Edit on the agent's checks for a member; the private one is editable.
    expect(screen.getByTestId('check-agent:coding').textContent).not.toContain('Edit')
    expect(screen.getByTestId('check-user:review').textContent).toContain('Edit')
  })

  it('removes a check only after the second click, on the right route', async () => {
    stub()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('check-user:review')).toBeTruthy())
    const rowEl = screen.getByTestId('check-user:review')
    fireEvent.click(Array.from(rowEl.querySelectorAll('button')).find((b) => b.textContent === 'Remove')!)
    fireEvent.click(screen.getByTestId('delete-user:review'))
    await waitFor(() => expect(fetchSpy.mock.calls.some(
      ([p, init]) => String(p).endsWith('/user-checks/review') && (init as RequestInit)?.method === 'DELETE',
    )).toBe(true))
  })

  it('saves a new check from the editor with the rubric as a judge section', async () => {
    stub()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('new-check')).toBeTruthy())
    fireEvent.click(screen.getByTestId('new-check'))
    fireEvent.change(screen.getByTestId('check-name'), { target: { value: 'tests' } })
    fireEvent.click(screen.getByTestId('check-mandatory'))
    // The condition block (kinds, actions, places, paths) folds away under "every turn".
    expect(screen.getByTestId('check-condition')).toBeTruthy()
    fireEvent.click(screen.getByTestId('check-always'))
    expect(screen.queryByTestId('check-condition')).toBeNull()
    fireEvent.change(screen.getByTestId('check-rubric'), { target: { value: 'Every change has a test.' } })
    fireEvent.click(screen.getByTestId('check-save'))
    await waitFor(() => expect(fetchSpy.mock.calls.some(
      ([p, init]) => String(p).endsWith('/checks/tests') && (init as RequestInit)?.method === 'PUT',
    )).toBe(true))
    const call = fetchSpy.mock.calls.find(([p]) => String(p).endsWith('/checks/tests'))!
    const body = JSON.parse(String((call[1] as RequestInit).body))
    expect(body.doc.mandatory).toBe(true)
    expect(body.doc.judge.rubric).toBe('Every change has a test.')
    expect(body.doc.condition).toEqual({ always: true })
    expect(body.doc.rounds).toBe(3)
    expect(body.script).toBeNull()
  })

  it('keeps the command patterns of a check it edits', async () => {
    const withCommands = {
      ...CHECKS,
      checks: CHECKS.checks.map((c) => (c.ref === 'user:review' ? {
        ...c, condition: { kinds: ['code'], commands: ['make\\s+deploy, prod'] },
        doc: { name: 'review', condition: { kinds: ['code'], commands: ['make\\s+deploy, prod'] }, judge: { rubric: 'r' } },
      } : c)),
    }
    stub(withCommands as typeof CHECKS)
    renderPage()
    await waitFor(() => expect(screen.getByTestId('check-user:review')).toBeTruthy())
    const rowEl = screen.getByTestId('check-user:review')
    fireEvent.click(Array.from(rowEl.querySelectorAll('button')).find((b) => b.textContent === 'Edit')!)
    fireEvent.click(screen.getByTestId('check-save'))
    await waitFor(() => expect(fetchSpy.mock.calls.some(
      ([p, init]) => String(p).endsWith('/user-checks/review') && (init as RequestInit)?.method === 'PUT',
    )).toBe(true))
    const call = fetchSpy.mock.calls.find(([p]) => String(p).endsWith('/user-checks/review'))!
    const body = JSON.parse(String((call[1] as RequestInit).body))
    expect(body.doc.condition).toEqual({ kinds: ['code'], commands: ['make\\s+deploy, prod'] })
  })

  it('does not save a daily cap that is not an amount', async () => {
    stub()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('check-cap')).toBeTruthy())
    const saveBtn = () => Array.from(screen.getByTestId('check-spend').querySelectorAll('button'))
      .find((b) => b.textContent?.trim() === 'Save') as HTMLButtonElement
    fireEvent.change(screen.getByTestId('check-cap'), { target: { value: 'ten' } })
    expect(saveBtn().disabled).toBe(true)
    fireEvent.change(screen.getByTestId('check-cap'), { target: { value: '12.5' } })
    expect(saveBtn().disabled).toBe(false)
  })

  it('refuses an empty check in the editor without a request', async () => {
    stub()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('new-private-check')).toBeTruthy())
    fireEvent.click(screen.getByTestId('new-private-check'))
    expect(screen.queryByTestId('check-mandatory')).toBeNull()
    fireEvent.change(screen.getByTestId('check-name'), { target: { value: 'empty' } })
    fireEvent.click(screen.getByTestId('check-save'))
    await waitFor(() => expect(screen.getByTestId('check-error').textContent).toContain('needs instructions for the judge'))
    expect(fetchSpy.mock.calls.some(([p]) => String(p).includes('/user-checks/'))).toBe(false)
  })
})
